"""
Archival for pipeline/audit-trail residue (raw_observations, proposed_observations,
review_items) that dominates tracker.db's size but is mostly already-resolved
history, not live weekly data.

Design constraint (deliberate, do not relax without re-deriving): "resolved" is
necessary but never sufficient for archival eligibility. A row is eligible only
when ALL FOUR hold:
  1. resolved   - disposition/status != 'pending'
  2. closed     - the week (or season) it belongs to is closed, per
                  weekly_projection_locks / seasons.projections_locked_at --
                  never per weeks.status (dead column, never written anywhere
                  in this app) and never per wall-clock age alone.
  3. dwelled    - resolved_at is at least DWELL_DAYS in the past.
  4. uncorrected - no row in `corrections` against its resulting record within
                  the dwell window (i.e. nobody has been actively fixing data
                  that traces back to this observation recently).

raw_observations has one more condition layered on top: a raw payload is only
eligible once EVERY proposed_observations row derived from it (there can be
many per PDF/pull) is independently eligible.

--report and --apply MUST call the same eligibility functions in this module.
There is deliberately no second code path for "preview" vs "actually do it".
"""
import sqlite3
import datetime
import json
import os

DWELL_DAYS_DEFAULT = 14

ARCHIVE_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "archive_schema.sql")


def get_archive_conn(archive_db_path):
    """Opens (creating + initializing schema if needed) the archive db.
    Never touches tracker.db."""
    conn = sqlite3.connect(archive_db_path)
    conn.row_factory = sqlite3.Row
    with open(ARCHIVE_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    return conn


def _now_iso():
    # timezone-aware UTC -- datetime.utcnow() is deprecated as of Python 3.12
    # (Justin's machine runs 3.13). Sliced to match the naive
    # datetime('now')-style timestamps already used throughout this schema
    # (e.g. resolved_at, created_at), so string comparison against those
    # columns stays a simple lexical comparison.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _dwell_cutoff_iso(as_of, dwell_days):
    as_of_dt = datetime.datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    return (as_of_dt - datetime.timedelta(days=dwell_days)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Condition 2: is this week/season "closed"?
# ---------------------------------------------------------------------------

def week_closed(live_conn, season_id, week_id):
    """
    week_id is None -> season-scope: closed iff this season's projections
    have been locked (seasons.projections_locked_at is set).

    week_id is not None -> weekly-scope: closed iff every source_name that
    has ANY proposed_observations row for this week_id also has a matching
    row in weekly_projection_locks for that week. If there are zero sources
    (no data at all for this week), treat as closed -- vacuously true, and
    there's nothing to archive there anyway.

    Deliberately does NOT read weeks.status -- confirmed by grep that no
    code path in this app ever writes it, so it would always read the seeded
    default and could never actually gate anything.
    """
    if week_id is None:
        row = live_conn.execute(
            "SELECT projections_locked_at FROM seasons WHERE season_id=?", (season_id,)
        ).fetchone()
        return bool(row and row["projections_locked_at"])

    sources = {
        r["source_name"]
        for r in live_conn.execute(
            "SELECT DISTINCT source_name FROM proposed_observations WHERE week_id=?", (week_id,)
        )
    }
    if not sources:
        return True
    locked = {
        r["source_name"]
        for r in live_conn.execute(
            "SELECT source_name FROM weekly_projection_locks WHERE week_id=?", (week_id,)
        )
    }
    return sources.issubset(locked)


# ---------------------------------------------------------------------------
# Condition 4: any recent correction against the record this observation
# produced?
# ---------------------------------------------------------------------------

_DATA_TYPE_TO_ENTITY_TYPE = {"actual": "statistic", "projection": "projection", "ranking": "ranking"}


def _has_recent_correction(live_conn, entity_type, entity_id, cutoff_iso):
    if entity_id is None:
        return False
    row = live_conn.execute(
        """SELECT 1 FROM corrections
           WHERE entity_type=? AND entity_id=? AND corrected_at >= ?
           LIMIT 1""",
        (entity_type, entity_id, cutoff_iso),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# proposed_observations eligibility
# ---------------------------------------------------------------------------

def eligible_proposed_observation_ids(live_conn, dwell_days=DWELL_DAYS_DEFAULT, as_of=None):
    """Returns the set of proposed_id values currently eligible for archival."""
    as_of = as_of or _now_iso()
    cutoff = _dwell_cutoff_iso(as_of, dwell_days)

    rows = live_conn.execute(
        """SELECT proposed_id, season_id, week_id, data_type, disposition,
                  resulting_record_id, resolved_at
           FROM proposed_observations
           WHERE disposition != 'pending' AND resolved_at IS NOT NULL AND resolved_at < ?""",
        (cutoff,),
    ).fetchall()

    eligible = set()
    for r in rows:
        if not week_closed(live_conn, r["season_id"], r["week_id"]):
            continue
        entity_type = _DATA_TYPE_TO_ENTITY_TYPE.get(r["data_type"])
        if entity_type and _has_recent_correction(live_conn, entity_type, r["resulting_record_id"], cutoff):
            continue
        eligible.add(r["proposed_id"])
    return eligible


# ---------------------------------------------------------------------------
# raw_observations eligibility
# ---------------------------------------------------------------------------

def eligible_raw_observation_ids(live_conn, dwell_days=DWELL_DAYS_DEFAULT, as_of=None,
                                  eligible_proposed_ids=None):
    """
    A raw_id is eligible only if:
      - it actually carries inline content worth reclaiming (raw_content IS NOT NULL)
      - it has at least one proposed_observations row derived from it (never
        archive on the theory that "nobody looked at it" -- if we can't
        positively confirm every downstream row is done with it, leave it live)
      - EVERY proposed_observations row derived from it is itself eligible

    The link to proposed_observations is via import_id, NOT raw_id.
    proposed_observations.raw_id exists in the schema for the AI-vision
    extraction path, but is never actually populated by the pipeline this
    project uses in practice (confirmed against live data: 0 of ~35k rows
    have it set -- screenshot ingestion this season went through manual
    transcription, not vision extraction). The pipeline's real, populated
    link between one raw payload and the observations it produced is that
    both raw_observations and proposed_observations carry the same
    import_id (one row per "The Athletic_week1", "ESPN_week3", etc. upload).
    """
    if eligible_proposed_ids is None:
        eligible_proposed_ids = eligible_proposed_observation_ids(live_conn, dwell_days, as_of)

    candidates = live_conn.execute(
        "SELECT raw_id, import_id FROM raw_observations WHERE raw_content IS NOT NULL"
    ).fetchall()

    eligible = set()
    for r in candidates:
        deps = [row["proposed_id"] for row in live_conn.execute(
            "SELECT proposed_id FROM proposed_observations WHERE import_id=?", (r["import_id"],)
        )]
        if not deps:
            continue  # unknown downstream state -- don't touch it
        if all(pid in eligible_proposed_ids for pid in deps):
            eligible.add(r["raw_id"])
    return eligible


# ---------------------------------------------------------------------------
# review_items eligibility
# ---------------------------------------------------------------------------

def eligible_review_item_ids(live_conn, dwell_days=DWELL_DAYS_DEFAULT, as_of=None,
                              eligible_proposed_ids=None):
    """
    entity_type='proposed_observation': eligible iff the referenced
    proposed_observations row is itself eligible AND this review_items row
    is resolved + dwelled.

    entity_type in ('statistic','ranking'): derive week/season from the
    referenced statistics/rankings row (those tables always stay live) and
    apply the same four conditions directly against this review_items row.

    Any other/unrecognized entity_type: never eligible (conservative default).
    """
    as_of = as_of or _now_iso()
    cutoff = _dwell_cutoff_iso(as_of, dwell_days)
    if eligible_proposed_ids is None:
        eligible_proposed_ids = eligible_proposed_observation_ids(live_conn, dwell_days, as_of)

    rows = live_conn.execute(
        """SELECT review_id, entity_type, entity_id, status, resolved_at
           FROM review_items
           WHERE status != 'pending' AND resolved_at IS NOT NULL AND resolved_at < ?""",
        (cutoff,),
    ).fetchall()

    eligible = set()
    for r in rows:
        if r["entity_type"] == "proposed_observation":
            if r["entity_id"] in eligible_proposed_ids:
                eligible.add(r["review_id"])
            continue

        if r["entity_type"] in ("statistic", "ranking"):
            table = "statistics" if r["entity_type"] == "statistic" else "rankings"
            id_col = "statistic_id" if r["entity_type"] == "statistic" else "ranking_id"
            target = live_conn.execute(
                f"SELECT season_id, week_id FROM {table} WHERE {id_col}=?", (r["entity_id"],)
            ).fetchone()
            if not target:
                continue  # dangling reference -- leave live, don't guess
            if not week_closed(live_conn, target["season_id"], target["week_id"]):
                continue
            if _has_recent_correction(live_conn, r["entity_type"], r["entity_id"], cutoff):
                continue
            eligible.add(r["review_id"])
        # else: unrecognized entity_type -- never eligible

    return eligible


# ---------------------------------------------------------------------------
# Reporting (read-only, no writes to either db)
# ---------------------------------------------------------------------------

def build_report(live_conn, dwell_days=DWELL_DAYS_DEFAULT, as_of=None):
    as_of = as_of or _now_iso()
    eligible_po = eligible_proposed_observation_ids(live_conn, dwell_days, as_of)
    eligible_ro = eligible_raw_observation_ids(live_conn, dwell_days, as_of, eligible_proposed_ids=eligible_po)
    eligible_ri = eligible_review_item_ids(live_conn, dwell_days, as_of, eligible_proposed_ids=eligible_po)

    def _bytes_for(table, id_col, ids, content_cols):
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cols = " + ".join(f"COALESCE(LENGTH({c}),0)" for c in content_cols)
        row = live_conn.execute(
            f"SELECT SUM({cols}) FROM {table} WHERE {id_col} IN ({placeholders})", list(ids)
        ).fetchone()
        return row[0] or 0

    report = {
        "as_of": as_of,
        "dwell_days": dwell_days,
        "raw_observations": {
            "eligible_count": len(eligible_ro),
            "total_count": live_conn.execute("SELECT COUNT(*) c FROM raw_observations").fetchone()["c"],
            "bytes_est": _bytes_for("raw_observations", "raw_id", eligible_ro, ["raw_content"]),
        },
        "proposed_observations": {
            "eligible_count": len(eligible_po),
            "total_count": live_conn.execute("SELECT COUNT(*) c FROM proposed_observations").fetchone()["c"],
            "bytes_est": _bytes_for(
                "proposed_observations", "proposed_id", eligible_po,
                ["extracted_player_name", "position", "data_type", "scope", "ranking_type",
                 "source_name", "stat_name", "validation_flags_json"],
            ),
        },
        "review_items": {
            "eligible_count": len(eligible_ri),
            "total_count": live_conn.execute("SELECT COUNT(*) c FROM review_items").fetchone()["c"],
            "bytes_est": _bytes_for(
                "review_items", "review_id", eligible_ri,
                ["entity_type", "reason", "confidence", "source_name", "details_json"],
            ),
        },
        "eligible_ids": {
            "raw_observations": sorted(eligible_ro),
            "proposed_observations": sorted(eligible_po),
            "review_items": sorted(eligible_ri),
        },
    }
    return report


def print_report(report):
    print(f"Archive eligibility report -- as of {report['as_of']} (dwell={report['dwell_days']}d)")
    print("=" * 72)
    for table in ("raw_observations", "proposed_observations", "review_items"):
        d = report[table]
        mb = d["bytes_est"] / (1024 * 1024)
        print(f"{table:24s} eligible {d['eligible_count']:6d} / {d['total_count']:6d}   ~{mb:6.2f} MB")
    total_mb = sum(report[t]["bytes_est"] for t in
                   ("raw_observations", "proposed_observations", "review_items")) / (1024 * 1024)
    print("-" * 72)
    print(f"{'estimated total reclaim':24s} ~{total_mb:.2f} MB")


# ---------------------------------------------------------------------------
# Apply (copy -> verify -> delete). Not invoked by --report. Writes to BOTH
# archive.db and tracker.db, and always writes an archive_manifest row.
# ---------------------------------------------------------------------------

_TABLE_COLUMNS = {
    "raw_observations": [
        "raw_id", "import_id", "collection_run_id", "content_type", "raw_content", "file_path", "created_at",
    ],
    "proposed_observations": [
        "proposed_id", "import_id", "raw_id", "extracted_player_name", "resolved_player_id",
        "identity_match_type", "position", "data_type", "scope", "ranking_type", "season_id", "week_id",
        "week_hint", "source_name", "stat_name", "stat_value", "field_confidence", "validation_flags_json",
        "disposition", "resulting_record_id", "created_at", "resolved_at",
    ],
    "review_items": [
        "review_id", "entity_type", "entity_id", "reason", "confidence", "source_name", "details_json",
        "status", "created_at", "resolved_at", "resolved_by",
    ],
}
_ID_COL = {"raw_observations": "raw_id", "proposed_observations": "proposed_id", "review_items": "review_id"}


def apply_archive(live_conn, archive_conn, table, ids, dry_run=False):
    """
    Copies the given rows from `table` in live_conn into the same table in
    archive_conn (same primary keys), verifies every column matches, then --
    only if dry_run is False and verification passed for every row -- deletes
    them from live_conn. Always records an archive_manifest row (dry_run flag
    included), even for a dry run, so the report and the action share one
    audit trail.

    Returns (rows_moved, bytes_reclaimed_est).
    """
    if table not in _TABLE_COLUMNS:
        raise ValueError(f"Unknown archivable table: {table}")
    if not ids:
        return 0, 0

    cols = _TABLE_COLUMNS[table]
    id_col = _ID_COL[table]
    started_at = _now_iso()

    placeholders = ",".join("?" * len(ids))
    live_rows = live_conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} WHERE {id_col} IN ({placeholders})", list(ids)
    ).fetchall()
    if len(live_rows) != len(ids):
        raise RuntimeError(
            f"{table}: expected {len(ids)} rows, found {len(live_rows)} -- refusing to proceed"
        )

    bytes_est = 0
    col_list = ", ".join(cols)
    placeholders_insert = ", ".join("?" * len(cols))
    verified_ids = []

    for row in live_rows:
        values = [row[c] for c in cols]
        bytes_est += sum(len(str(v)) for v in values if v is not None)
        archive_conn.execute(
            f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders_insert})", values
        )
        archived_back = archive_conn.execute(
            f"SELECT {col_list} FROM {table} WHERE {id_col}=?", (row[id_col],)
        ).fetchone()
        if tuple(archived_back) != tuple(values):
            raise RuntimeError(f"{table} id={row[id_col]}: archive verification mismatch, aborting")
        verified_ids.append(row[id_col])

    if not dry_run:
        archive_conn.commit()
        del_placeholders = ",".join("?" * len(verified_ids))
        live_conn.execute(f"DELETE FROM {table} WHERE {id_col} IN ({del_placeholders})", verified_ids)
        live_conn.commit()
    else:
        archive_conn.rollback()

    finished_at = _now_iso()
    archive_conn.execute(
        """INSERT INTO archive_manifest
           (run_started_at, run_finished_at, table_name, rows_moved, bytes_reclaimed_est,
            min_id, max_id, week_ids_json, dry_run, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (started_at, finished_at, table, len(verified_ids), bytes_est,
         min(verified_ids), max(verified_ids), json.dumps(sorted(set(ids))), int(dry_run),
         None),
    )
    archive_conn.commit()

    return len(verified_ids), bytes_est
