"""
One-off migration for the 2026-10-07 lock-gap decision. DRY RUN BY DEFAULT.

What it does (all in ONE transaction, committed only if every check passes):

  1. Finds current scope='weekly' projection rows with is_frozen=0 whose
     source+week has a row in weekly_projection_locks. Authoritative lock
     state decides -- NOT created_at > lock_date (which misses lock-day
     arrivals and mishandles UTC-vs-local dates).
  2. For each: sets is_frozen=1, appends the late-after-lock tag to notes
     (so accuracy comparisons exclude it), and writes an audit row to
     `corrections` (entity_type='projection', field_name='is_frozen', 0 -> 1).
     projected_value is NEVER touched. Nothing is deleted.
  3. Reverts Week 4's weeks.status from 'completed' back to 'upcoming'
     (the 2026-10-07 resolve script was the only writer that ever set it; the
     column is display-only and not maintained -- see db/archive.py).
  4. Before committing, re-reads every projection_id/projected_value pair in
     the table and compares a SHA-256 to the pre-migration value; any
     difference aborts and rolls back.

--apply additionally takes a backup of tracker.db first (refuses to overwrite
an existing backup file). Run from the repo root:

    python migrate_freeze_late_weekly_projections.py                 # dry run, read-only
    python migrate_freeze_late_weekly_projections.py --apply         # writes
"""
import argparse
import datetime
import hashlib
import os
import sqlite3
import sys

from db import repository as repo

DB_PATH = "db/tracker.db"
STATUS_REVERT_WEEK = 4
SEASON_YEAR = 2026

_GAP_WHERE = """
    p.scope='weekly' AND p.is_current=1 AND p.is_frozen=0
    AND EXISTS (SELECT 1 FROM weekly_projection_locks l
                WHERE l.week_id=p.week_id
                  AND l.source_name IN (p.source_name, REPLACE(p.source_name, ' (live)', '')))
"""


def find_gap_rows(conn):
    return conn.execute(
        f"""SELECT p.projection_id, p.week_id, w.week_number, p.source_name, p.stat_name,
                   p.projected_value, p.created_at, p.notes
            FROM projections p JOIN weeks w ON w.week_id=p.week_id
            WHERE {_GAP_WHERE}
            ORDER BY w.week_number, p.source_name, p.player_id, p.stat_name"""
    ).fetchall()


def values_fingerprint(conn):
    """SHA-256 over every (projection_id, projected_value) pair in the table."""
    h = hashlib.sha256()
    for r in conn.execute("SELECT projection_id, projected_value FROM projections ORDER BY projection_id"):
        h.update(f"{r[0]}:{r[1]!r};".encode())
    return h.hexdigest()


def historical_post_lock_count(conn):
    """Informational only: already-frozen current weekly rows in locked source/weeks
    that are provably created after their lock. The comparison eligibility rule
    already excludes these at read time; the migration does not touch them."""
    n = 0
    for r in conn.execute(
        """SELECT p.created_at, p.notes, l.locked_at
           FROM projections p
           JOIN weekly_projection_locks l ON l.week_id=p.week_id
                AND l.source_name IN (p.source_name, REPLACE(p.source_name, ' (live)', ''))
           WHERE p.scope='weekly' AND p.is_current=1 AND p.is_frozen=1"""):
        if repo.projection_created_after_lock(r["created_at"], r["locked_at"]):
            n += 1
    return n


def _week_status_row(conn):
    return conn.execute(
        """SELECT w.week_id, w.status FROM weeks w JOIN seasons s ON s.season_id=w.season_id
           WHERE s.year=? AND w.week_number=? AND w.week_type='regular'""",
        (SEASON_YEAR, STATUS_REVERT_WEEK),
    ).fetchone()


def plan(conn):
    rows = find_gap_rows(conn)
    groups = {}
    for r in rows:
        key = (r["source_name"], r["week_number"], (r["created_at"] or "")[:10])
        g = groups.setdefault(key, {"rows": 0, "players": set()})
        g["rows"] += 1
    wk = _week_status_row(conn)
    return {
        "rows": rows,
        "groups": groups,
        "week_status": dict(wk) if wk else None,
        "historical_post_lock": historical_post_lock_count(conn),
    }


def apply_migration(conn, rows):
    """Performs steps 2-4 inside one transaction on `conn`. Raises (and rolls back)
    on any integrity problem. Returns a summary dict."""
    before = values_fingerprint(conn)
    frozen = 0
    try:
        conn.execute("BEGIN")
        for r in rows:
            notes = r["notes"]
            if not notes:
                new_notes = repo.LATE_AFTER_LOCK_TAG
            elif repo.LATE_AFTER_LOCK_TAG in notes:
                new_notes = notes
            else:
                new_notes = f"{notes} {repo.LATE_AFTER_LOCK_TAG}"
            cur = conn.execute(
                "UPDATE projections SET is_frozen=1, notes=? WHERE projection_id=? AND is_frozen=0",
                (new_notes, r["projection_id"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"projection_id={r['projection_id']} was not updated exactly once")
            conn.execute(
                """INSERT INTO corrections (entity_type, entity_id, field_name, original_value, corrected_value,
                                            corrected_by, reason)
                   VALUES ('projection', ?, 'is_frozen', '0', '1', 'migration',
                           'froze late-after-lock weekly projection (source+week lock gap, 2026-10-07)')""",
                (r["projection_id"],),
            )
            frozen += 1

        status_reverted = 0
        wk = _week_status_row(conn)
        if wk and wk["status"] == "completed":
            status_reverted = conn.execute(
                "UPDATE weeks SET status='upcoming' WHERE week_id=? AND status='completed'", (wk["week_id"],)
            ).rowcount

        after = values_fingerprint(conn)
        if before != after:
            raise RuntimeError("projected_value fingerprint changed -- aborting, rolling back")
        leftover = len(find_gap_rows(conn))
        if leftover:
            raise RuntimeError(f"{leftover} gap rows still unfrozen after migration -- aborting, rolling back")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"frozen": frozen, "status_reverted": status_reverted, "fingerprint": after}


def backup_db(path):
    stamp = datetime.date.today().strftime("%Y%m%d")
    dest = f"{path}.bak-{stamp}-pre-lockgap"
    if os.path.exists(dest):
        raise SystemExit(f"[ABORT] backup already exists, refusing to overwrite: {dest}")
    src = sqlite3.connect(path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return dest


EXPECTED_WEEKS = {4}
EXPECTED_SOURCES = {"ESPN", "Yahoo"}


def unexpected_rows(rows):
    """Rows outside the 2026-10-07 decision's scope (Week 4, ESPN/Yahoo). The
    migration refuses to --apply when any exist unless --allow-unexpected is given."""
    return [r for r in rows
            if r["week_number"] not in EXPECTED_WEEKS
            or r["source_name"].replace(" (live)", "") not in EXPECTED_SOURCES]


def print_plan(p):
    rows = p["rows"]
    print(f"Gap rows (current, weekly, unfrozen, source+week locked): {len(rows)}")
    by_source = {}
    for r in rows:
        by_source[r["source_name"]] = by_source.get(r["source_name"], 0) + 1
    print("By source:", ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())) or "(none)")
    print("By (source, week, created-date UTC):")
    for (src, wk, day), g in sorted(p["groups"].items()):
        print(f"  {src:14s} week {wk}  {day}  rows={g['rows']}")
    print()
    print(f"Correction (audit) records that would be written: {len(rows)}")
    print("projected_value changes: 0 by construction (--apply verifies with a SHA-256 over every "
          "projection_id/projected_value pair and rolls back on any difference)")
    unexpected = unexpected_rows(rows)
    print(f"Unexpected rows (outside Week {sorted(EXPECTED_WEEKS)} / {sorted(EXPECTED_SOURCES)}): {len(unexpected)}")
    for r in unexpected[:10]:
        print(f"  UNEXPECTED projection_id={r['projection_id']} wk{r['week_number']} {r['source_name']} {r['stat_name']}")
    print()
    print("Sample (first 10): projection_id, week, source, stat, value, created_at, notes")
    for r in rows[:10]:
        print(f"  {r['projection_id']}, wk{r['week_number']}, {r['source_name']}, {r['stat_name']}, "
              f"{r['projected_value']}, {r['created_at']}, {r['notes']!r}")
    print()
    print(f"Week {STATUS_REVERT_WEEK} status now: {p['week_status']}")
    print(f"Already-frozen rows provably created after their lock (not touched; excluded from comparisons "
          f"by the eligibility rule at read time): {p['historical_post_lock']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default is a read-only dry run)")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--allow-unexpected", action="store_true",
                    help="apply even if gap rows exist outside Week 4 / ESPN / Yahoo")
    args = ap.parse_args()

    if not args.apply:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        print("DRY RUN -- read-only, nothing will be written. Re-run with --apply to migrate.\n")
        print_plan(plan(conn))
        conn.close()
        return

    conn = sqlite3.connect(args.db, isolation_level=None)  # explicit BEGIN/COMMIT in apply_migration
    conn.row_factory = sqlite3.Row
    p = plan(conn)
    print_plan(p)
    print()
    if unexpected_rows(p["rows"]) and not args.allow_unexpected:
        conn.close()
        raise SystemExit("[ABORT] unexpected rows found (see above); nothing written. "
                         "Review them, or re-run with --allow-unexpected.")
    dest = backup_db(args.db)
    print(f"Backup written: {dest}")
    result = apply_migration(conn, p["rows"])
    print(f"Frozen + tagged + audited: {result['frozen']} rows")
    print(f"Week {STATUS_REVERT_WEEK} status reverted: {result['status_reverted']}")
    print(f"projected_value fingerprint unchanged: {result['fingerprint']}")
    print(f"Gap rows remaining: {len(find_gap_rows(conn))}")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
