"""
Core data-access layer: player identity resolution, provenance-preserving
writes for statistics/projections/rankings, and the correction/audit trail.
No fabrication, no silent overwrites (sec. 44).
"""
from db.database import get_conn, normalize_name
import json

# Lightweight data-integrity checks (spec sec. 19). Deliberately minimal:
# catches obviously-impossible values without rejecting unusual-but-real
# NFL statistics (e.g. yardage totals are NOT restricted to non-negative,
# since sack-adjusted or safety-adjusted totals can occasionally be negative).
NON_NEGATIVE_STATS = {"receptions", "pass_tds", "rush_tds", "receiving_tds",
                       "fumbles_lost", "interceptions", "pass_completions", "pass_attempts"}


def validate_stat_value(conn, stat_name, value):
    """Raises ValueError on an obviously-impossible value. None (missing) always passes.

    Integrity fix (2026-09-07): previously this only checked non-negativity for a
    known subset of stat_names and never checked whether stat_name was a real,
    recognized raw-stat category at all. That gap let an arbitrary label —
    concretely, a pre-computed 'fantasy_points' total pulled from a screenshot —
    pass validation, auto-accept, and land directly in the trusted `projections`
    table, silently violating the "raw stats only, points always derived, never
    stored" invariant enforced everywhere else in this project. Membership is
    checked against stat_definitions (the same source of truth
    ingest_collector_batch._malformed_reason already uses for the collector-batch
    path), not a second hardcoded list, so the two paths can't drift apart.
    """
    if value is None:
        return
    known_stat_names = {r["stat_name"] for r in conn.execute("SELECT DISTINCT stat_name FROM stat_definitions")}
    if stat_name not in known_stat_names:
        raise ValueError(f"unrecognized stat_name '{stat_name}' — not a raw stat category; "
                          f"fantasy points and other derived totals are never stored directly")
    if stat_name in NON_NEGATIVE_STATS and value < 0:
        raise ValueError(f"{stat_name} cannot be negative (got {value})")


def validate_ranking_value(value):
    """Phase 4C prerequisite. Ranking values must be positive whole numbers —
    rank 1 is best. None (missing) always passes, same convention as
    validate_stat_value."""
    if value is None:
        return
    if value != int(value):
        raise ValueError(f"ranking value must be a whole number (got {value})")
    if value < 1:
        raise ValueError(f"ranking value must be 1 or greater — rank 1 is best (got {value})")


def find_source_id(conn, name):
    row = conn.execute("SELECT source_id FROM sources WHERE name = ?", (name,)).fetchone()
    return row["source_id"] if row else None


def resolve_player(conn, display_name, position, team_abbr=None):
    """
    Attempt to resolve a name to an existing player.
    Returns (player_id, match_type) where match_type is one of:
      'exact'      - unambiguous match on normalized name + position
      'alias'      - matched via a known alias
      'none'       - no match found (caller should create or send to review)
      'ambiguous'  - multiple candidates found; caller MUST send to review, never guess (sec. 8)
    """
    norm = normalize_name(display_name)

    exact = conn.execute(
        "SELECT player_id FROM players WHERE normalized_name = ? AND position = ?",
        (norm, position),
    ).fetchall()
    if len(exact) == 1:
        return exact[0]["player_id"], "exact"
    if len(exact) > 1:
        return None, "ambiguous"

    alias = conn.execute(
        """SELECT DISTINCT p.player_id FROM player_name_aliases a
           JOIN players p ON p.player_id = a.player_id
           WHERE a.normalized_alias = ? AND p.position = ?""",
        (norm, position),
    ).fetchall()
    if len(alias) == 1:
        return alias[0]["player_id"], "alias"
    if len(alias) > 1:
        return None, "ambiguous"

    return None, "none"


def create_player(conn, display_name, position, external_ids_json=None):
    norm = normalize_name(display_name)
    cur = conn.execute(
        "INSERT INTO players (display_name, normalized_name, position, external_ids_json) VALUES (?, ?, ?, ?)",
        (display_name, norm, position, external_ids_json),
    )
    conn.commit()
    return cur.lastrowid


def add_statistic(conn, player_id, season_id, week_id, stat_name, stat_value,
                   source_name, game_id=None, team_id=None, value_state="observed",
                   observation_timestamp=None, verification_status="unverified",
                   provenance_ref=None, collection_run_id=None):
    """
    Insert a new statistic observation. Never overwrites an existing observation;
    if one exists for the same identity (player, season, week, stat, source) with
    a different value, this creates a NEW version and flags a conflict for review
    rather than silently choosing one (sec. 13, 18, 24).
    """
    validate_stat_value(conn, stat_name, stat_value)
    source_id = find_source_id(conn, source_name)
    if source_id is None:
        raise ValueError(f"Unknown source: {source_name}")

    existing = conn.execute(
        """SELECT * FROM statistics WHERE player_id=? AND season_id=? AND week_id=?
           AND stat_name=? AND source_id=? AND is_current=1""",
        (player_id, season_id, week_id, stat_name, source_id),
    ).fetchone()

    version = 1
    is_conflict = False
    if existing:
        if existing["stat_value"] == stat_value and existing["value_state"] == value_state:
            # Identical observation already recorded — idempotent no-op (sec. 24)
            return existing["statistic_id"], False
        # Differing observation: preserve the old one, insert a new version, flag conflict.
        # The new row is inserted BEFORE the old one is marked non-current and BEFORE the
        # review item is created, so the review item can reference both statistic_ids —
        # this is what lets a "reject" decision actually revert trust (see
        # resolve_statistic_conflict below). Previously only the old id was recorded,
        # so a reviewer's decision had no effect on which value was trusted.
        version = existing["observation_version"] + 1
        is_conflict = True

    cur = conn.execute(
        """INSERT INTO statistics
           (player_id, season_id, week_id, game_id, team_id, stat_name, stat_value, value_state,
            source_id, observation_timestamp, verification_status, observation_version,
            provenance_ref, collection_run_id, is_current)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (player_id, season_id, week_id, game_id, team_id, stat_name, stat_value, value_state,
         source_id, observation_timestamp, verification_status, version, provenance_ref, collection_run_id),
    )
    new_id = cur.lastrowid

    if is_conflict:
        conn.execute("UPDATE statistics SET is_current=0 WHERE statistic_id=?", (existing["statistic_id"],))
        details = json.dumps({
            "previous_statistic_id": existing["statistic_id"],
            "previous_value": existing["stat_value"],
            "new_statistic_id": new_id,
            "new_value": stat_value,
            "source": source_name,
        })
        conn.execute(
            """INSERT INTO review_items (entity_type, entity_id, reason, confidence, source_name, details_json, status)
               VALUES ('statistic', ?, 'conflict', NULL, ?, ?, 'pending')""",
            (new_id, source_name, details),
        )
    conn.commit()
    return cur.lastrowid, True


def resolve_statistic_conflict(conn, review_id, action, resolved_by="user"):
    """
    Resolve a pending statistic conflict from the Review Queue.

    Audit finding (Phase 2 readiness review): previously, approving or
    rejecting a review item only updated review_items.status — the newly
    inserted (disputed) statistic stayed is_current=1 regardless of the
    reviewer's decision, so "reject" had no actual effect on trusted data.
    This function makes the review decision authoritative:
      - 'approved': the new value remains trusted (no change to is_current).
      - 'rejected': trust reverts to the previous value (previous row's
        is_current is restored to 1, the disputed row's is_current is set
        to 0). Neither row is deleted — full history is preserved either way.
    Every resolution is written to `corrections` for audit purposes,
    regardless of outcome. This is the ONLY conflict-resolution path for
    statistics; there is no separate fantasy-point-specific resolution system.
    """
    if action not in ("approved", "rejected"):
        raise ValueError(f"Unknown resolution action: {action}")

    item = conn.execute("SELECT * FROM review_items WHERE review_id=?", (review_id,)).fetchone()
    if not item or item["entity_type"] != "statistic" or item["reason"] != "conflict":
        raise ValueError("Not a resolvable statistic conflict review item")

    details = json.loads(item["details_json"])
    prev_id = details["previous_statistic_id"]
    new_id = details["new_statistic_id"]

    if action == "rejected":
        conn.execute("UPDATE statistics SET is_current=0 WHERE statistic_id=?", (new_id,))
        conn.execute("UPDATE statistics SET is_current=1 WHERE statistic_id=?", (prev_id,))
        conn.execute(
            """INSERT INTO corrections (entity_type, entity_id, field_name, original_value, corrected_value, corrected_by, reason)
               VALUES ('statistic', ?, 'is_current', '1', '0', ?, 'conflict rejected via Review Queue — reverted to prior trusted value')""",
            (new_id, resolved_by),
        )
    else:
        conn.execute(
            """INSERT INTO corrections (entity_type, entity_id, field_name, original_value, corrected_value, corrected_by, reason)
               VALUES ('statistic', ?, 'verification_status', 'unverified', 'verified', ?, 'conflict approved via Review Queue — new value confirmed trusted')""",
            (new_id, resolved_by),
        )
        conn.execute("UPDATE statistics SET verification_status='verified' WHERE statistic_id=?", (new_id,))

    conn.execute(
        "UPDATE review_items SET status=?, resolved_at=datetime('now'), resolved_by=? WHERE review_id=?",
        (action, resolved_by, review_id),
    )
    conn.commit()


def validate_scope_week_consistency(scope, week_id):
    """Phase 4C season-scope extension. scope='weekly' must have a real week;
    scope='season' must not — the two are mutually exclusive, and week_id
    being NULL should never be silently interpreted as scope on its own
    (same principle as ranking_type being a real field, not inferred)."""
    if scope not in ("weekly", "season"):
        raise ValueError(f"scope must be 'weekly' or 'season' (got {scope!r})")
    if scope == "weekly" and week_id is None:
        raise ValueError("scope='weekly' requires a week_id")
    if scope == "season" and week_id is not None:
        raise ValueError("scope='season' must not have a week_id")


def set_season_start_date(conn, season_id, start_date):
    """start_date: 'YYYY-MM-DD' string, the real calendar date Week 1 begins.
    Never guessed or defaulted — must be provided explicitly."""
    import datetime
    try:
        datetime.date.fromisoformat(start_date)
    except ValueError:
        raise ValueError(f"start_date must be in YYYY-MM-DD format (got {start_date!r})")
    conn.execute("UPDATE seasons SET start_date=? WHERE season_id=?", (start_date, season_id))
    conn.commit()


def ensure_season_lock(conn, season_id, today=None):
    """
    Idempotent check-and-lock: if the season has a start_date, today is on or
    after it, and locking hasn't already happened, freeze every currently-
    current scope='season' projection for this season. Once frozen, a row's
    value can never be silently superseded (see add_projection) — this is
    what makes "compare against actual stats at season's end" meaningful:
    the frozen baseline is guaranteed stable from lock day forward.

    Cheap and safe to call from anywhere (a write path, a page load) — does
    nothing once already locked, and does nothing if start_date is unset.
    Returns True if this call performed the lock, False otherwise.
    """
    import datetime
    today = today or datetime.date.today().isoformat()
    season = conn.execute("SELECT * FROM seasons WHERE season_id=?", (season_id,)).fetchone()
    if not season or not season["start_date"] or season["projections_locked_at"]:
        return False
    if today < season["start_date"]:
        return False
    conn.execute(
        "UPDATE projections SET is_frozen=1 WHERE season_id=? AND scope='season' AND is_current=1",
        (season_id,),
    )
    conn.execute("UPDATE seasons SET projections_locked_at=? WHERE season_id=?", (today, season_id))
    conn.commit()
    return True


def ensure_weekly_projection_lock(conn, source_name, week_id, today=None):
    """
    Idempotent check-and-lock, mirroring ensure_season_lock but scoped per
    (source_name, week_id) rather than per season -- explicit 2026-09-16
    policy decision: a source's weekly projections lock the moment its
    FIRST successful ingestion for that week completes, not when the week
    or games begin. Different sources upload on different days, so this
    can't live on the shared `weeks` row -- it's tracked per source in
    weekly_projection_locks.

    Freezes every currently-current scope='weekly' projection for this
    source+week. Safe to call after every successful weekly ingest -- a
    no-op once already locked (checked via weekly_projection_locks, not by
    scanning for an already-frozen row, so a week with zero auto-accepted
    rows on its first run -- everything sent to review -- still locks
    correctly and doesn't look "never locked" to a later call).
    Returns True if this call performed the lock, False otherwise.
    """
    import datetime
    today = today or datetime.date.today().isoformat()
    already = conn.execute(
        "SELECT 1 FROM weekly_projection_locks WHERE source_name=? AND week_id=?",
        (source_name, week_id),
    ).fetchone()
    if already:
        return False
    conn.execute(
        "UPDATE projections SET is_frozen=1 WHERE source_name=? AND week_id=? AND scope='weekly' AND is_current=1",
        (source_name, week_id),
    )
    conn.execute(
        "INSERT INTO weekly_projection_locks (source_name, week_id, locked_at) VALUES (?, ?, ?)",
        (source_name, week_id, today),
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Late-after-lock handling (2026-10-07 decision)
#
# ensure_weekly_projection_lock freezes the rows that exist when a source's
# first ingestion for a week completes, and records the lock in
# weekly_projection_locks. add_projection's frozen check only fires when an
# EXISTING current row for the same player/stat/source/week is frozen, so a
# brand-new identity arriving after the lock (a player added to the roster
# later, a review-queue acceptance) used to be inserted unfrozen -- and, being
# captured after the lock (possibly after the games), was not a valid
# pre-game projection for accuracy comparison either.
#
# Two separate guarantees, deliberately not conflated:
#   1. Immutability: such a row is now stored frozen (is_frozen=1).
#   2. Comparison validity: such a row is tagged LATE_AFTER_LOCK_TAG in notes
#      at insert time and is excluded from accuracy comparisons by
#      is_projection_eligible_for_comparison. Freezing stops later mutation;
#      it does not make a post-lock capture a legitimate pre-lock forecast.
# ---------------------------------------------------------------------------

LATE_AFTER_LOCK_TAG = "[late-after-lock]"


def _lock_source_candidates(source_name):
    """A row's source_name may carry the ' (live)' suffix; its lock row is
    keyed by the base source (same mapping freeze_late_weekly_rows.py uses)."""
    base = source_name.replace(" (live)", "") if source_name else source_name
    return (source_name, base)


def get_weekly_projection_lock(conn, source_name, week_id):
    """The weekly_projection_locks row governing this source+week, or None.
    Authoritative lock state -- never inferred from timestamps."""
    if not source_name or week_id is None:
        return None
    exact, base = _lock_source_candidates(source_name)
    return conn.execute(
        "SELECT source_name, week_id, locked_at FROM weekly_projection_locks "
        "WHERE week_id=? AND source_name IN (?, ?)",
        (week_id, exact, base),
    ).fetchone()


def _parse_utc(ts):
    import datetime
    return datetime.datetime.strptime(ts.strip().replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=datetime.timezone.utc)


def projection_created_after_lock(created_at, locked_at, local_tz=None):
    """
    True only when a row's created_at is provably AFTER the lock.

    Actual lock semantics in this project (do not approximate):
      - projections.created_at is UTC (SQLite datetime('now')).
      - weekly_projection_locks.locked_at, as written by
        ensure_weekly_projection_lock, is a DATE ONLY ('YYYY-MM-DD') taken from
        datetime.date.today() -- the machine's LOCAL calendar date, no time of day.
      - A full 'YYYY-MM-DD HH:MM:SS' locked_at is interpreted as UTC (the same
        convention as created_at), so a future precise lock compares exactly.

    Date-only lock: created_at is converted to the local calendar (local_tz, or
    the system timezone when None -- the same clock date.today() used) and is
    "after" only if that local date is strictly later than the lock date. A
    naive date(created_at) > locked_at comparison is wrong here: a row created
    at 8pm local on lock day already has the next UTC date. Rows created on
    the lock day itself are NOT provably post-lock from timestamps alone (the
    lock has no time of day); the insert-time tag, not this function, is what
    catches those.
    """
    if not created_at or not locked_at:
        return False
    import datetime
    created = _parse_utc(created_at)
    locked_at = locked_at.strip()
    if len(locked_at) <= 10:
        local_date = created.astimezone(local_tz).date() if local_tz else created.astimezone().date()
        return local_date > datetime.date.fromisoformat(locked_at)
    return created > _parse_utc(locked_at)


def is_projection_eligible_for_comparison(notes, created_at, locked_at, local_tz=None):
    """A weekly projection may enter an accuracy comparison only if it was not
    tagged late-after-lock at insert time AND is not provably created after its
    source+week lock. locked_at=None (no lock for this source/week) means no
    lock-based exclusion applies."""
    if notes and LATE_AFTER_LOCK_TAG in notes:
        return False
    return not projection_created_after_lock(created_at, locked_at, local_tz)


def add_projection(conn, player_id, season_id, week_id, stat_name, projected_value,
                    source_type="user", source_name=None, notes=None, is_frozen=False, provenance_ref=None,
                    scope="weekly", today=None):
    """
    Insert a projection. If a projection already exists for this identity and the
    week has NOT begun, this supersedes it while preserving the original (sec. 15).
    If the week HAS begun (is_frozen on existing record), the original is preserved
    and this becomes a distinct, timestamped correction — never silently overwritten.

    scope='season' (Phase 4C season-scope extension) represents a season-long/
    preseason projection with no single applicable week — week_id must be None
    in that case. Identity lookup uses "week_id IS ?" rather than "week_id=?":
    SQL's three-valued logic means "week_id = NULL" is never true, so a bare
    equality would silently defeat idempotency/versioning for every season-scope
    row (this exact class of bug was already fixed once for ranking_type).

    Identity is also scoped by source_name (via "source_name IS ?", same NULL-
    safety reasoning as week_id above) — audit finding: this was previously
    missing entirely, meaning two different sources' independent projections
    for the same player/stat/week were treated as competing versions of ONE
    projection, with the second source silently "correcting" the first rather
    than being stored as a separate observation. That directly broke the
    per-source comparison this multi-source ingestion work exists to enable.
    statistics and rankings already scoped identity by source correctly from
    the start (sec. 12); this brings projections in line with that same
    invariant rather than leaving it as a lingering asymmetry.

    Idempotency: if the existing current value for this identity is
    numerically identical to the new one, this is a no-op — returns the
    existing row without creating a spurious new version. Audit finding:
    this had never been true for add_projection (unlike add_statistic/
    add_ranking, which both already no-op on an identical re-entry) — a
    real, concrete problem once re-imports of unchanged source data started
    actually happening: re-running the exact same real ESPN page created
    116 meaningless "version 2" rows with the identical value as version 1,
    pure audit-trail noise with no real correction behind it.

    Locking: if the existing current row for this identity is_frozen (either a
    weekly projection whose week has begun, or — the more consequential case —
    a season projection frozen by ensure_season_lock at the season's start),
    this raises ValueError rather than silently creating a new version. Once
    frozen, a projection's value is genuinely immutable, not just flagged.
    """
    validate_stat_value(conn, stat_name, projected_value)
    validate_scope_week_consistency(scope, week_id)
    if scope == "season":
        ensure_season_lock(conn, season_id, today=today)
    existing = conn.execute(
        """SELECT * FROM projections WHERE player_id=? AND season_id=? AND week_id IS ?
           AND stat_name=? AND scope=? AND source_name IS ? AND is_current=1""",
        (player_id, season_id, week_id, stat_name, scope, source_name),
    ).fetchone()

    if existing and existing["projected_value"] == projected_value:
        # Idempotent no-op takes priority over the frozen check: nothing is
        # actually being modified, so re-processing the same real data after
        # a season locks (e.g. a harmless re-import) shouldn't raise an
        # error — only a genuine attempted change to frozen data should.
        return existing["projection_id"], False

    if existing and existing["is_frozen"]:
        reason = ("the season has started and season-long projections are locked for end-of-season comparison"
                   if scope == "season" else "the week has already begun")
        raise ValueError(f"Cannot modify this projection — {reason}.")

    # Late-after-lock: a new row for a source+week that is already locked is
    # stored frozen and tagged (see the block comment above). Runs after the
    # idempotent/frozen checks so existing lock behavior is unchanged, and
    # never fires for scope='season' or sourceless (manual 'User') rows.
    if scope == "weekly" and get_weekly_projection_lock(conn, source_name, week_id) is not None:
        is_frozen = True
        if not notes:
            notes = LATE_AFTER_LOCK_TAG
        elif LATE_AFTER_LOCK_TAG not in notes:
            notes = f"{notes} {LATE_AFTER_LOCK_TAG}"

    version = 1
    if existing:
        version = existing["observation_version"] + 1
        cur = conn.execute(
            """INSERT INTO projections
               (player_id, season_id, week_id, scope, stat_name, projected_value, source_type, source_name,
                notes, provenance_ref, observation_version, is_frozen, is_current)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (player_id, season_id, week_id, scope, stat_name, projected_value, source_type, source_name,
             notes, provenance_ref, version, int(is_frozen)),
        )
        new_id = cur.lastrowid
        conn.execute(
            "UPDATE projections SET is_current=0, superseded_by=? WHERE projection_id=?",
            (new_id, existing["projection_id"]),
        )
        conn.execute(
            """INSERT INTO corrections (entity_type, entity_id, field_name, original_value, corrected_value, reason)
               VALUES ('projection', ?, 'projected_value', ?, ?, ?)""",
            (existing["projection_id"], str(existing["projected_value"]), str(projected_value),
             "week already begun" if existing["is_frozen"] else "user correction"),
        )
        conn.commit()
        return new_id, True

    cur = conn.execute(
        """INSERT INTO projections
           (player_id, season_id, week_id, scope, stat_name, projected_value, source_type, source_name,
            notes, provenance_ref, observation_version, is_frozen, is_current)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (player_id, season_id, week_id, scope, stat_name, projected_value, source_type, source_name,
         notes, provenance_ref, version, int(is_frozen)),
    )
    conn.commit()
    return cur.lastrowid, True


def add_ranking(conn, player_id, season_id, week_id, source_name, ranking_value, ranking_type,
                 position=None, source_type="manual", publication_timestamp=None,
                 verification_status="unverified", notes=None, collection_run_id=None, provenance_ref=None,
                 scope="weekly"):
    """

    Insert a ranking observation. Identical duplicate re-entries are idempotent
    no-ops (same convention as add_statistic). A DIFFERING value for an
    already-current (player, season, week, source, ranking_type) preserves the
    original, inserts a new version, and flags a conflict for review — Phase
    4C prerequisite: previously this silently superseded without any review,
    which was inconsistent with how statistics/projections handle exactly the
    same situation. Approving/rejecting that conflict goes through
    resolve_ranking_conflict, mirroring resolve_statistic_conflict.

    scope='season' (Phase 4C season-scope extension): a preseason/draft-prep
    ranking with no single applicable week — week_id must be None. Identity
    lookup uses "week_id IS ?", not "week_id=?" — see add_projection's
    docstring for why a bare equality would silently break this for every
    season-scope row.
    """
    validate_ranking_value(ranking_value)
    validate_scope_week_consistency(scope, week_id)
    source_id = find_source_id(conn, source_name)
    if source_id is None:
        raise ValueError(f"Unknown source: {source_name}")

    existing = conn.execute(
        """SELECT * FROM rankings WHERE player_id=? AND season_id=? AND week_id IS ?
           AND source_id=? AND ranking_type=? AND scope=? AND is_current=1""",
        (player_id, season_id, week_id, source_id, ranking_type, scope),
    ).fetchone()

    version = 1
    is_conflict = False
    if existing:
        if existing["ranking_value"] == ranking_value:
            return existing["ranking_id"], False
        version = existing["observation_version"] + 1
        is_conflict = True

    cur = conn.execute(
        """INSERT INTO rankings
           (player_id, season_id, week_id, scope, source_id, ranking_value, ranking_type, position,
            publication_timestamp, source_type, verification_status, notes, observation_version,
            collection_run_id, provenance_ref, is_current)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (player_id, season_id, week_id, scope, source_id, ranking_value, ranking_type, position,
         publication_timestamp, source_type, verification_status, notes, version,
         collection_run_id, provenance_ref),
    )
    new_id = cur.lastrowid

    if is_conflict:
        conn.execute("UPDATE rankings SET is_current=0 WHERE ranking_id=?", (existing["ranking_id"],))
        details = json.dumps({
            "previous_ranking_id": existing["ranking_id"],
            "previous_value": existing["ranking_value"],
            "new_ranking_id": new_id,
            "new_value": ranking_value,
            "ranking_type": ranking_type,
            "source": source_name,
        })
        conn.execute(
            """INSERT INTO review_items (entity_type, entity_id, reason, confidence, source_name, details_json, status)
               VALUES ('ranking', ?, 'conflict', NULL, ?, ?, 'pending')""",
            (new_id, source_name, details),
        )
    conn.commit()
    return new_id, True


def resolve_ranking_conflict(conn, review_id, action, resolved_by="user"):
    """Mirrors resolve_statistic_conflict exactly, for the same reason: approving
    or rejecting a ranking conflict must actually change which value is trusted,
    not just update review_items.status cosmetically."""
    if action not in ("approved", "rejected"):
        raise ValueError(f"Unknown resolution action: {action}")

    item = conn.execute("SELECT * FROM review_items WHERE review_id=?", (review_id,)).fetchone()
    if not item or item["entity_type"] != "ranking" or item["reason"] != "conflict":
        raise ValueError("Not a resolvable ranking conflict review item")

    details = json.loads(item["details_json"])
    prev_id = details["previous_ranking_id"]
    new_id = details["new_ranking_id"]

    if action == "rejected":
        conn.execute("UPDATE rankings SET is_current=0 WHERE ranking_id=?", (new_id,))
        conn.execute("UPDATE rankings SET is_current=1 WHERE ranking_id=?", (prev_id,))
        conn.execute(
            """INSERT INTO corrections (entity_type, entity_id, field_name, original_value, corrected_value, corrected_by, reason)
               VALUES ('ranking', ?, 'is_current', '1', '0', ?, 'conflict rejected via Review Queue — reverted to prior trusted value')""",
            (new_id, resolved_by),
        )
    else:
        conn.execute(
            """INSERT INTO corrections (entity_type, entity_id, field_name, original_value, corrected_value, corrected_by, reason)
               VALUES ('ranking', ?, 'verification_status', 'unverified', 'verified', ?, 'conflict approved via Review Queue — new value confirmed trusted')""",
            (new_id, resolved_by),
        )
        conn.execute("UPDATE rankings SET verification_status='verified' WHERE ranking_id=?", (new_id,))

    conn.execute(
        "UPDATE review_items SET status=?, resolved_at=datetime('now'), resolved_by=? WHERE review_id=?",
        (action, resolved_by, review_id),
    )
    conn.commit()


def correct_value(conn, entity_type, entity_id, field_name, table, id_field, original_value, corrected_value, reason=None):
    """Generic correction recorder used for direct edits to already-current records
       (e.g. fixing a typo'd verification_status). Full value corrections for
       statistics/projections/rankings should go through add_* above so history
       is preserved as new rows, not just an audit log entry."""
    conn.execute(
        f"UPDATE {table} SET {field_name} = ? WHERE {id_field} = ?", (corrected_value, entity_id)
    )
    conn.execute(
        """INSERT INTO corrections (entity_type, entity_id, field_name, original_value, corrected_value, reason)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (entity_type, entity_id, field_name, str(original_value), str(corrected_value), reason),
    )
    conn.commit()
