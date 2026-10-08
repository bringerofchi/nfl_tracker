"""
Phase 3 ingestion pipeline.

    Screenshot
        -> Raw Observation      (immutable, imports + raw_observations, never edited/deleted)
        -> Extraction            (pluggable Extractor, per-field confidence)
        -> Validation             (hard: db.repository.validate_stat_value; soft: plausibility/week-consistency)
        -> Confidence routing     (auto-accept vs Review Queue)
        -> Trusted Statistic      (via the SAME repo.add_statistic/add_projection every other
                                    source uses — inherits versioning + conflict detection for free)
        -> Fantasy Service        (unchanged; recalculates from whatever is now trusted)

Nothing here writes directly into `statistics` or `projections`. Every write
goes through db.repository, which is also what manual entry uses — this is
what makes screenshot extraction "another source feeding the architecture"
rather than a bolted-on, isolated feature with its own rules.

An AI extraction is never authoritative merely because its confidence score
crossed a threshold: auto-accept additionally requires an unambiguous
resolved player identity and a clean hard-validation/plausibility pass. If
either of those fails, the field goes to the Review Queue regardless of how
confident the extractor was.
"""
import hashlib
import json
import os
import sqlite3
from typing import Optional

from db import repository as repo
from ingestion.extractor import Extractor, ExtractorUnavailable
from ingestion.validation import plausibility_flags, week_consistency_flags, completions_attempts_flags
from ingestion.collector import AdapterUnavailable

AUTO_ACCEPT_CONFIDENCE_THRESHOLD = 0.85


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def create_import(conn, file_path: str, file_name: str, file_type: str) -> int:
    """Immutable raw upload record. Created BEFORE extraction runs and never
    modified by anything downstream — the original artifact is always
    recoverable regardless of what the AI made of it."""
    file_hash = _file_hash(file_path)
    cur = conn.execute(
        """INSERT INTO imports (file_name, file_path, file_hash, file_type, status)
           VALUES (?, ?, ?, ?, 'uploaded')""",
        (file_name, file_path, file_hash, file_type),
    )
    import_id = cur.lastrowid
    conn.execute(
        """INSERT INTO raw_observations (import_id, content_type, file_path)
           VALUES (?, 'file', ?)""",
        (import_id, file_path),
    )
    conn.commit()
    return import_id


def _route_field(conn, import_id: int, player_name_raw: str, resolved_player_id, identity_match_type: str,
                  position: Optional[str], data_type: str, season_id: int, week_id, week_number,
                  week_hint, source_name: str, stat_name: str, value, confidence: float,
                  flags: list, auto_accept_threshold: float, ranking_type: Optional[str] = None,
                  scope: str = "weekly") -> tuple:
    """
    Shared routing core for a single normalized field, regardless of where it
    came from (screenshot extraction or a collector batch) or what kind of
    observation it is (actual/projection/ranking — Phase 4C prerequisite).
    This is the one place that decides auto-accept vs Review Queue and
    performs the actual write via db.repository — every ingestion entry point
    calls this so there is exactly one contract, not several diverging ones.

    ranking_type is only meaningful when data_type='ranking'; it's stored as
    '' otherwise (never NULL — see the proposed_observations schema comment
    on why NULL would silently break the pending-duplicate unique index).

    scope='season' (Phase 4C season-scope extension): a season-long/preseason
    observation with no applicable week_id/week_number. Only 'projection' and
    'ranking' data_types support scope='season' — actual stats are always
    tied to a specific game/week, so data_type='actual' with scope='season'
    is treated as a hard validation failure, same as any other invalid value.

    Returns (disposition, proposed_id) where disposition is 'auto_accepted',
    'sent_to_review', or 'duplicate_race'.
    """
    hard_invalid_reason = None
    if data_type == "actual" and scope == "season":
        hard_invalid_reason = "data_type='actual' does not support scope='season' — actual stats are always tied to a specific week"
    else:
        try:
            if data_type == "ranking":
                repo.validate_ranking_value(value)
            else:
                repo.validate_stat_value(conn, stat_name, value)
            repo.validate_scope_week_consistency(scope, week_id)
        except ValueError as e:
            hard_invalid_reason = str(e)

    stored_ranking_type = ranking_type if data_type == "ranking" else ""

    cur = conn.execute(
        """INSERT INTO proposed_observations
           (import_id, raw_id, extracted_player_name, resolved_player_id, identity_match_type,
            position, data_type, ranking_type, scope, season_id, week_id, week_hint, source_name, stat_name, stat_value,
            field_confidence, validation_flags_json, disposition)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (import_id, None, player_name_raw, resolved_player_id, identity_match_type,
         position, data_type, stored_ranking_type, scope, season_id, week_id, week_hint, source_name,
         stat_name, value, confidence, json.dumps(flags), "pending"),
    )
    proposed_id = cur.lastrowid

    identity_ok = identity_match_type in ("exact", "alias") and resolved_player_id is not None
    week_ok = (scope == "weekly" and week_id is not None) or (scope == "season" and data_type != "actual")
    can_auto_accept = (
        identity_ok
        and value is not None
        and confidence >= auto_accept_threshold
        and hard_invalid_reason is None
        and not flags
        and week_ok
    )

    if can_auto_accept:
        if data_type == "actual":
            new_id, _ = repo.add_statistic(conn, resolved_player_id, season_id, week_id, stat_name, value,
                                            source_name=source_name, verification_status="unverified",
                                            provenance_ref=f"import:{import_id}")
        elif data_type == "ranking":
            new_id, _ = repo.add_ranking(conn, resolved_player_id, season_id, week_id, source_name, value,
                                          ranking_type, position=position, source_type="automated",
                                          verification_status="unverified", provenance_ref=f"import:{import_id}",
                                          scope=scope)
        else:
            new_id, _ = repo.add_projection(conn, resolved_player_id, season_id, week_id, stat_name, value,
                                          source_type="ai_extracted", source_name=source_name,
                                          notes=f"auto-accepted from import {import_id}",
                                          provenance_ref=f"import:{import_id}", scope=scope)
        conn.execute(
            "UPDATE proposed_observations SET disposition='auto_accepted', resulting_record_id=?, resolved_at=datetime('now') WHERE proposed_id=?",
            (new_id, proposed_id),
        )
        return "auto_accepted", proposed_id

    reason = ("ambiguous_identity" if identity_match_type == "ambiguous" else
              "player_not_found" if identity_match_type == "none" else
              "invalid_value" if hard_invalid_reason else
              "low_confidence" if confidence < auto_accept_threshold else
              "needs_review")
    confidence_label = "High" if confidence >= 0.85 else "Medium" if confidence >= 0.6 else "Low"
    details = {
        "extracted_player_name": player_name_raw,
        "identity_match_type": identity_match_type,
        "resolved_player_id": resolved_player_id,
        "stat_name": stat_name,
        "stat_value": value,
        "ranking_type": ranking_type if data_type == "ranking" else None,
        "field_confidence": confidence,
        "week_number": week_number,
        "scope": scope,
        "source_name": source_name,
        "data_type": data_type,
        "flags": flags,
        "hard_invalid_reason": hard_invalid_reason,
        "proposed_id": proposed_id,
    }
    conn.execute(
        """INSERT INTO review_items (entity_type, entity_id, reason, confidence, source_name, details_json, status)
           VALUES ('proposed_observation', ?, ?, ?, ?, ?, 'pending')""",
        (proposed_id, reason, confidence_label, source_name, json.dumps(details)),
    )
    try:
        conn.execute("UPDATE proposed_observations SET disposition='sent_to_review' WHERE proposed_id=?", (proposed_id,))
    except sqlite3.IntegrityError:
        # DB-level race guard fired (idx_unique_pending_proposal): another concurrent
        # run already created an identical pending review item between our earlier
        # app-level duplicate check and this write. Roll back to a clean state for
        # THIS proposal — mark it rejected-as-duplicate rather than leaving it stuck
        # mid-transition, and remove the review item we just inserted for it so the
        # queue doesn't show two entries for one underlying conflict.
        conn.execute(
            "DELETE FROM review_items WHERE entity_type='proposed_observation' AND entity_id=? AND status='pending'",
            (proposed_id,),
        )
        conn.execute(
            "UPDATE proposed_observations SET disposition='rejected', resolved_at=datetime('now') WHERE proposed_id=?",
            (proposed_id,),
        )
        return "duplicate_race", proposed_id
    return "sent_to_review", proposed_id


def ingest_screenshot(conn, extractor: Extractor, file_path: str, file_name: str,
                       season_id: int, week_number: int, data_type: str, source_name: str,
                       position_hint: Optional[str] = None,
                       auto_accept_threshold: float = AUTO_ACCEPT_CONFIDENCE_THRESHOLD) -> dict:
    """
    Runs one screenshot through the full pipeline. Returns a summary dict:
    {import_id, extraction_failed, auto_accepted: [...], sent_to_review: [...]}
    """
    if data_type not in ("actual", "projection"):
        raise ValueError("data_type must be 'actual' or 'projection'")

    import_id = create_import(conn, file_path, file_name, file_type="screenshot")
    week_row = conn.execute(
        "SELECT week_id FROM weeks WHERE season_id=? AND week_number=?", (season_id, week_number)
    ).fetchone()
    week_id = week_row["week_id"] if week_row else None

    try:
        extraction = extractor.extract(file_path, position_hint=position_hint)
    except ExtractorUnavailable as e:
        conn.execute("UPDATE imports SET status='failed', extraction_method=? WHERE import_id=?",
                     (type(extractor).__name__, import_id))
        conn.execute(
            """INSERT INTO review_items (entity_type, entity_id, reason, confidence, details_json, status)
               VALUES ('import', ?, 'source_problem', NULL, ?, 'pending')""",
            (import_id, json.dumps({"error": str(e), "file_name": file_name})),
        )
        conn.commit()
        return {"import_id": import_id, "extraction_failed": True, "error": str(e),
                "auto_accepted": [], "sent_to_review": []}

    # Preserve the raw extraction result itself, independent of any normalized
    # proposed_observations row derived from it (raw extraction -> proposed
    # observation -> trusted statistic must stay three distinct, inspectable stages).
    conn.execute(
        """INSERT INTO raw_observations (import_id, content_type, raw_content) VALUES (?, 'ai_extraction_result', ?)""",
        (import_id, json.dumps({
            "player_name_raw": extraction.player_name_raw,
            "player_name_confidence": extraction.player_name_confidence,
            "position_hint": extraction.position_hint,
            "week_hint": extraction.week_hint,
            "team_hint": extraction.team_hint,
            "source_context": extraction.source_context,
            "fields": [{"stat_name": f.stat_name, "value": f.value, "confidence": f.confidence} for f in extraction.fields],
        })),
    )
    conn.execute("UPDATE imports SET status='extracted', extraction_timestamp=datetime('now'), "
                 "extraction_method=? WHERE import_id=?", (type(extractor).__name__, import_id))

    # Identity is resolved ONCE per screenshot, not per field — every extracted
    # stat on this screenshot refers to the same player.
    effective_position = extraction.position_hint or position_hint
    if effective_position:
        resolved_player_id, identity_match_type = repo.resolve_player(
            conn, extraction.player_name_raw, effective_position
        )
    else:
        resolved_player_id, identity_match_type = None, "none"  # can't resolve without a position

    wk_flags = week_consistency_flags(week_number, extraction.week_hint)
    # Cross-field consistency (completions <= attempts) needs both values at once.
    comp = next((f.value for f in extraction.fields if f.stat_name == "pass_completions"), None)
    att = next((f.value for f in extraction.fields if f.stat_name == "pass_attempts"), None)
    ca_flags = completions_attempts_flags(comp, att)

    auto_accepted, sent_to_review, duplicates_skipped = [], [], []

    for f in extraction.fields:
        flags = list(wk_flags) + plausibility_flags(f.stat_name, f.value)
        if f.stat_name in ("pass_completions", "pass_attempts"):
            flags += ca_flags
        disposition, proposed_id = _route_field(
            conn, import_id, extraction.player_name_raw, resolved_player_id, identity_match_type,
            effective_position, data_type, season_id, week_id, week_number, extraction.week_hint,
            source_name, f.stat_name, f.value, f.confidence, flags, auto_accept_threshold,
        )
        if disposition == "auto_accepted":
            auto_accepted.append(proposed_id)
        elif disposition == "sent_to_review":
            sent_to_review.append(proposed_id)
        else:  # duplicate_race
            duplicates_skipped.append(proposed_id)

    conn.commit()
    return {"import_id": import_id, "extraction_failed": False,
            "auto_accepted": auto_accepted, "sent_to_review": sent_to_review,
            "duplicates_skipped": duplicates_skipped}


def _malformed_reason(obs, known_stat_names: set) -> Optional[str]:
    if not obs.player_name_raw:
        return "missing player_name"
    if obs.data_type not in ("actual", "projection", "ranking"):
        return f"unrecognized data_type '{obs.data_type}'"
    if obs.scope not in ("weekly", "season"):
        return f"unrecognized scope '{obs.scope}'"
    if obs.data_type == "actual" and obs.scope == "season":
        return "data_type='actual' does not support scope='season'"
    if obs.scope == "weekly" and obs.week_number is None:
        return "scope='weekly' requires a week_number"
    if obs.scope == "season" and obs.week_number is not None:
        return "scope='season' must not have a week_number"
    if obs.data_type == "ranking":
        if obs.ranking_type not in ("overall", "QB", "RB", "WR", "TE", "FLEX"):
            return f"unrecognized ranking_type '{obs.ranking_type}'"
    elif obs.stat_name not in known_stat_names:
        return f"unrecognized stat_name '{obs.stat_name}'"
    if obs.value is not None and not isinstance(obs.value, (int, float)):
        return f"non-numeric value {obs.value!r}"
    return None


def ingest_collector_batch(conn, adapter, season_id: int, week_number: int,
                            auto_accept_threshold: float = AUTO_ACCEPT_CONFIDENCE_THRESHOLD) -> dict:
    """
    Runs one SourceAdapter fetch through the SAME pipeline ingest_screenshot()
    uses (via _route_field), tracked as a collection_runs entry. Returns:
    {collection_run_id, import_id, auto_accepted: [...], sent_to_review: [...],
     duplicates_skipped: [...], errors: [...]}

    Differences from screenshot ingestion, both intentional:
    - Identity is resolved PER OBSERVATION (a batch covers many players),
      not once for the whole batch.
    - A duplicate-pending-review guard exists here (not in ingest_screenshot):
      a scheduled collector may re-fetch the same not-yet-reviewed data on a
      later run before a human gets to it. Re-flagging the identical pending
      item would spam the Review Queue, so an exact (player, week, stat,
      value) match against an existing pending review item is skipped rather
      than duplicated. This does NOT affect auto-accepted values — those
      already route through repo.add_statistic's own idempotent no-op for an
      identical current value.
    """
    source_row = conn.execute("SELECT source_id FROM sources WHERE name=?", (adapter.source_name,)).fetchone()
    if not source_row:
        # Bad job configuration, not an operational failure — return gracefully like
        # every other "expected" outcome so a scheduler orchestrating many jobs
        # doesn't need a special case for this one. No collection_runs row is
        # created (there's nothing valid to record a run against).
        return {"collection_run_id": None, "import_id": None, "fetch_failed": True, "config_error": True,
                "error": f"Unknown source: {adapter.source_name}",
                "auto_accepted": [], "sent_to_review": [], "duplicates_skipped": [], "errors": []}
    week_row = conn.execute(
        "SELECT week_id FROM weeks WHERE season_id=? AND week_number=?", (season_id, week_number)
    ).fetchone()
    week_id = week_row["week_id"] if week_row else None

    try:
        run_cur = conn.execute(
            """INSERT INTO collection_runs (source_id, season_id, week_id, status, collector_version)
               VALUES (?, ?, ?, 'started', ?)""",
            (source_row["source_id"], season_id, week_id, type(adapter).__name__),
        )
    except sqlite3.IntegrityError:
        # idx_unique_active_collection_run fired: a run for this exact
        # (source, season, week) is already in progress. Skip this attempt
        # entirely rather than racing it — this IS the run-locking guarantee,
        # enforced at the DB level so two schedulers/processes can't both start.
        # Explicit rollback matters here: the failed INSERT leaves an implicit
        # transaction open: without rolling it back, THIS connection would keep
        # holding a lock on the database file even though nothing was written,
        # capable of blocking the very run it just deferred to.
        conn.rollback()
        return {"collection_run_id": None, "import_id": None, "fetch_failed": False, "run_locked": True,
                "auto_accepted": [], "sent_to_review": [], "duplicates_skipped": [], "errors": []}
    collection_run_id = run_cur.lastrowid
    conn.commit()

    try:
        observations = adapter.fetch(season_id, week_number)
    except AdapterUnavailable as e:
        failure_type = "transient" if e.transient else "permanent"
        conn.execute(
            "UPDATE collection_runs SET status='failed', failure_type=?, completed_at=datetime('now'), notes=? WHERE run_id=?",
            (failure_type, str(e), collection_run_id),
        )
        conn.commit()
        return {"collection_run_id": collection_run_id, "import_id": None, "fetch_failed": True, "error": str(e),
                "failure_type": failure_type,
                "auto_accepted": [], "sent_to_review": [], "duplicates_skipped": [], "errors": []}

    # Immutable raw record of exactly what the source returned, before any parsing/normalization judgment calls.
    payload_json = json.dumps([{
        "player_name": o.player_name_raw, "position": o.position_hint, "week_number": o.week_number,
        "data_type": o.data_type, "stat_name": o.stat_name, "value": o.value, "confidence": o.confidence,
        "team": o.team_hint,
    } for o in observations])
    import_cur = conn.execute(
        """INSERT INTO imports (file_name, file_path, file_hash, file_type, status, extraction_method)
           VALUES (?, '', ?, 'collector_batch', 'extracted', ?)""",
        (f"{adapter.source_name}_week{week_number}", hashlib.sha256(payload_json.encode()).hexdigest(),
         type(adapter).__name__),
    )
    import_id = import_cur.lastrowid
    conn.execute(
        "INSERT INTO raw_observations (import_id, collection_run_id, content_type, raw_content) VALUES (?, ?, 'raw_json', ?)",
        (import_id, collection_run_id, payload_json),
    )

    known_stat_names = {r["stat_name"] for r in conn.execute("SELECT DISTINCT stat_name FROM stat_definitions")}

    auto_accepted, sent_to_review, duplicates_skipped, errors = [], [], [], []

    try:
        for obs in observations:
            malformed = _malformed_reason(obs, known_stat_names)
            if malformed:
                errors.append({"observation": obs.player_name_raw, "stat_name": obs.stat_name, "reason": malformed})
                continue

            if obs.scope == "season":
                obs_week_id = None  # season-scope observations have no applicable week, by construction
            else:
                obs_week_row = conn.execute(
                    "SELECT week_id FROM weeks WHERE season_id=? AND week_number=?", (season_id, obs.week_number)
                ).fetchone()
                obs_week_id = obs_week_row["week_id"] if obs_week_row else None

            if obs.position_hint:
                resolved_player_id, identity_match_type = repo.resolve_player(conn, obs.player_name_raw, obs.position_hint)
            else:
                resolved_player_id, identity_match_type = None, "none"

            flags = plausibility_flags(obs.stat_name, obs.value, scope=obs.scope)
            obs_ranking_type = obs.ranking_type if obs.data_type == "ranking" else ""
            would_auto_accept_identity = identity_match_type in ("exact", "alias") and resolved_player_id is not None
            would_auto_accept_week = (
                (obs.scope == "weekly" and obs_week_id is not None)
                or (obs.scope == "season" and obs.data_type != "actual")
            )
            would_auto_accept = (
                would_auto_accept_identity and obs.value is not None and obs.confidence >= auto_accept_threshold
                and not flags and would_auto_accept_week
            )
            if not would_auto_accept:
                # App-level fast path: skip the (validation + insert + rollback) round-trip
                # in the common non-racing case. The DB-level unique index in _route_field
                # is what actually guarantees no duplicate under concurrent execution.
                # ranking_type is part of the match — otherwise a WR ranking of 5 and an
                # overall ranking of 5 for the same player/week would be wrongly treated
                # as the same pending duplicate. "week_id IS ?" (not "=") because
                # season-scope rows have week_id=NULL, and SQL's three-valued logic means
                # "week_id = NULL" is never true — a bare equality would silently defeat
                # this dedup check for every season-scope observation.
                dup = conn.execute(
                    """SELECT po.proposed_id FROM proposed_observations po
                       JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
                       WHERE po.extracted_player_name=? AND po.week_id IS ? AND po.stat_name=? AND po.stat_value=?
                         AND po.ranking_type=? AND po.disposition='sent_to_review' AND ri.status='pending'""",
                    (obs.player_name_raw, obs_week_id, obs.stat_name, obs.value, obs_ranking_type),
                ).fetchone()
                if dup:
                    duplicates_skipped.append(dup["proposed_id"])
                    continue

            try:
                disposition, proposed_id = _route_field(
                    conn, import_id, obs.player_name_raw, resolved_player_id, identity_match_type,
                    obs.position_hint, obs.data_type, season_id, obs_week_id, obs.week_number, None,
                    adapter.source_name, obs.stat_name, obs.value, obs.confidence, flags, auto_accept_threshold,
                    ranking_type=obs.ranking_type, scope=obs.scope,
                )
            except ValueError as e:
                # Per-row error isolation (app invariant: batch ingestion skips a
                # failing row rather than aborting the whole batch). Concretely,
                # this is what a frozen-projection conflict raises -- either the
                # season lock (ensure_season_lock) or the per-source weekly lock
                # (ensure_weekly_projection_lock, applied at the end of this
                # function) -- and one locked row must not sacrifice every other
                # observation in the same fetch just because it landed later in
                # the loop than the lock-triggering row.
                errors.append({"observation": obs.player_name_raw, "stat_name": obs.stat_name, "reason": str(e)})
                continue
            if disposition == "auto_accepted":
                auto_accepted.append(proposed_id)
            elif disposition == "sent_to_review":
                sent_to_review.append(proposed_id)
            else:  # duplicate_race — DB-level guard caught what the app-level check missed
                duplicates_skipped.append(proposed_id)
    except Exception as e:
        # Anything unexpected here (a bug, a DB error) must not leave collection_runs
        # stuck at 'started' forever — that would break a future scheduler's ability
        # to tell a crashed run from a genuinely still-running one (run locking,
        # safe restart). Whatever was already committed for individual observations
        # stays committed (each is independently valid — repo.add_statistic only
        # commits fields that passed all checks), but the RUN's own bookkeeping
        # always reaches a terminal status.
        #
        # This returns a graceful result rather than re-raising (a change from the
        # original Phase 2/3 audit fix): a scheduler orchestrating many jobs needs
        # every call to come back as a normal result it can act on, not sometimes
        # throw — the failure is still fully visible via collection_runs.status='error'
        # and the returned 'error' field, nothing is hidden.
        #
        # A hard process kill (not a Python exception) cannot be caught here at all;
        # a scheduler must separately detect and reconcile runs stuck at 'started'
        # past a staleness threshold (see find_stale_collection_runs below).
        conn.execute(
            "UPDATE collection_runs SET status='error', completed_at=datetime('now'), notes=? WHERE run_id=?",
            (f"Unexpected error during processing: {e}", collection_run_id),
        )
        conn.commit()
        return {"collection_run_id": collection_run_id, "import_id": import_id, "fetch_failed": False,
                "processing_error": True, "error": str(e),
                "auto_accepted": auto_accepted, "sent_to_review": sent_to_review,
                "duplicates_skipped": duplicates_skipped, "errors": errors}

    status = "successful" if not errors else ("partial" if (auto_accepted or sent_to_review) else "failed")
    conn.execute(
        "UPDATE collection_runs SET status=?, completed_at=datetime('now'), notes=? WHERE run_id=?",
        (status, json.dumps({"errors": len(errors), "auto_accepted": len(auto_accepted),
                              "sent_to_review": len(sent_to_review), "duplicates_skipped": len(duplicates_skipped)}),
         collection_run_id),
    )
    conn.commit()

    # 2026-09-16 policy: a source's weekly projections lock the moment its
    # FIRST successful ingestion for that week completes -- not when the
    # week/games begin. Applied here (not inside the loop) so it fires once
    # per run regardless of how many weekly-projection rows it touched, and
    # only for a genuine weekly-scoped run (week_id is None for a season-scope
    # fetch, which this lock does not apply to -- see ensure_season_lock).
    if week_id is not None:
        repo.ensure_weekly_projection_lock(conn, adapter.source_name, week_id)

    return {"collection_run_id": collection_run_id, "import_id": import_id, "fetch_failed": False,
            "auto_accepted": auto_accepted, "sent_to_review": sent_to_review,
            "duplicates_skipped": duplicates_skipped, "errors": errors}


def find_stale_collection_runs(conn, stale_minutes: int = 60) -> list:
    """
    Read-only visibility helper for a future Phase 4B scheduler. A collection_run
    stuck at status='started' for longer than `stale_minutes` most likely means
    the process was killed outright (not a Python exception — those are now
    caught above and always reach a terminal status). This function does NOT
    resolve or retry anything itself; a scheduler decides what to do (mark
    failed, retry, alert) since that policy belongs to Phase 4B, not the
    ingestion pipeline.
    """
    rows = conn.execute(
        """SELECT * FROM collection_runs
           WHERE status='started' AND started_at <= datetime('now', ?)
           ORDER BY started_at""",
        (f"-{stale_minutes} minutes",),
    ).fetchall()
    return [dict(r) for r in rows]


def accept_proposed_observation(conn, proposed_id: int, corrected_value: Optional[float] = None,
                                 player_id_override=None, new_player_name: Optional[str] = None):
    """
    Human review: accept, optionally correcting the value and/or resolving
    identity. `player_id_override` may be an int (an existing player the
    reviewer picked) or the literal string 'CREATE_NEW' (paired with
    new_player_name) to create a new player at review time — the pipeline
    never creates a player on its own, only a human confirming one does.
    """
    row = conn.execute("SELECT * FROM proposed_observations WHERE proposed_id=?", (proposed_id,)).fetchone()
    if not row:
        raise ValueError("Unknown proposed observation")
    if row["disposition"] not in ("pending", "sent_to_review"):
        raise ValueError(f"Proposed observation already resolved (disposition={row['disposition']})")

    player_id = row["resolved_player_id"]
    if player_id_override == "CREATE_NEW":
        if not new_player_name:
            raise ValueError("new_player_name is required to create a new player")
        player_id = repo.create_player(conn, new_player_name, row["position"] or "WR")
    elif player_id_override is not None:
        player_id = int(player_id_override)

    if player_id is None:
        raise ValueError("Cannot accept without a resolved player — select an existing player or create one")
    if row["scope"] == "weekly" and row["week_id"] is None:
        raise ValueError("Cannot accept — the asserted week does not match a known week")

    final_value = corrected_value if corrected_value is not None else row["stat_value"]
    if final_value is None:
        raise ValueError("Cannot accept a null stat value — provide a corrected value")
    if row["data_type"] == "ranking":
        repo.validate_ranking_value(final_value)  # re-validate; propagates ValueError if still invalid
    else:
        repo.validate_stat_value(conn, row["stat_name"], final_value)  # re-validate; propagates ValueError if still invalid

    if row["data_type"] == "actual":
        new_id, _ = repo.add_statistic(conn, player_id, row["season_id"], row["week_id"], row["stat_name"], final_value,
                                        source_name=row["source_name"], verification_status="verified",
                                        provenance_ref=f"import:{row['import_id']} (reviewed)")
    elif row["data_type"] == "ranking":
        new_id, _ = repo.add_ranking(conn, player_id, row["season_id"], row["week_id"], row["source_name"], final_value,
                                      row["ranking_type"], position=row["position"], source_type="automated",
                                      verification_status="verified",
                                      provenance_ref=f"import:{row['import_id']} (reviewed)", scope=row["scope"])
    else:
        new_id, _ = repo.add_projection(conn, player_id, row["season_id"], row["week_id"], row["stat_name"], final_value,
                                      source_type="ai_extracted", source_name=row["source_name"],
                                      notes=f"accepted after review from import {row['import_id']}",
                                      provenance_ref=f"import:{row['import_id']} (reviewed)", scope=row["scope"])

    disposition = "corrected_and_accepted" if (corrected_value is not None or player_id_override is not None) else "accepted"
    conn.execute(
        "UPDATE proposed_observations SET disposition=?, resulting_record_id=?, resolved_player_id=?, resolved_at=datetime('now') WHERE proposed_id=?",
        (disposition, new_id, player_id, proposed_id),
    )
    conn.execute(
        "UPDATE review_items SET status='approved', resolved_at=datetime('now'), resolved_by='user' "
        "WHERE entity_type='proposed_observation' AND entity_id=? AND status='pending'",
        (proposed_id,),
    )
    conn.commit()
    return new_id


def reject_proposed_observation(conn, proposed_id: int, reason: Optional[str] = None):
    """Reject: no statistic/projection is created. The raw upload, raw
    extraction result, and this proposed_observations row are all preserved
    (never deleted) for audit purposes."""
    row = conn.execute("SELECT * FROM proposed_observations WHERE proposed_id=?", (proposed_id,)).fetchone()
    if not row:
        raise ValueError("Unknown proposed observation")
    conn.execute(
        "UPDATE proposed_observations SET disposition='rejected', resolved_at=datetime('now') WHERE proposed_id=?",
        (proposed_id,),
    )
    conn.execute(
        "UPDATE review_items SET status='rejected', resolved_at=datetime('now'), resolved_by='user' "
        "WHERE entity_type='proposed_observation' AND entity_id=? AND status='pending'",
        (proposed_id,),
    )
    conn.commit()
