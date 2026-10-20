"""
Point sources.is_canonical_actuals at the source that actually holds the actuals.

Why: fantasy/service.py reads actual stats ONLY from the source flagged
is_canonical_actuals='yes'. The flag is on 'NFL', but every actuals row on file
(weeks 1-5) was ingested under 'ESPN', so every projected-vs-actual comparison
currently returns "no actual" (0 comparable player-weeks).

Safe by default:
  python scripts/set_canonical_actuals_source.py             -> dry run, changes nothing
  python scripts/set_canonical_actuals_source.py --apply     -> backup, then one transaction, then validate

Only the is_canonical_actuals flag (and updated_at) on two rows of `sources` changes.
No statistics, projections or lock rows are touched.
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("NFL_TRACKER_DB_PATH") or os.path.join(REPO_ROOT, "db", "tracker.db")


def snapshot(conn):
    rows = conn.execute(
        """SELECT so.source_id, so.name, so.is_canonical_actuals,
                  (SELECT COUNT(*) FROM statistics s
                    WHERE s.source_id = so.source_id AND s.is_current = 1) AS actual_rows,
                  (SELECT COUNT(DISTINCT s.week_id) FROM statistics s
                    WHERE s.source_id = so.source_id AND s.is_current = 1) AS actual_weeks
             FROM sources so ORDER BY so.source_id"""
    ).fetchall()
    return rows


def print_snapshot(title, rows):
    print(title)
    for r in rows:
        print(f"  id={r[0]} {r[1]:<14} canonical={r[2]:<3} current actual rows={r[3]} weeks={r[4]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="ESPN", help="source name to make canonical (default ESPN)")
    ap.add_argument("--apply", action="store_true", help="actually make the change (default is dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    before = snapshot(conn)
    print(f"DB: {DB_PATH}")
    print_snapshot("BEFORE", before)

    target = next((r for r in before if r[1] == args.to), None)
    if target is None:
        print(f"ERROR: no source named {args.to!r}")
        return 1
    if target[3] == 0:
        print(f"ERROR: {args.to} has no current actuals; refusing to make it canonical")
        return 1
    current = [r for r in before if r[2] == "yes"]
    if len(current) == 1 and current[0][1] == args.to:
        print("Already canonical; nothing to do.")
        return 0

    print(f"\nPLAN: canonical actuals source {[r[1] for r in current]} -> {args.to}")
    if not args.apply:
        print("DRY RUN: nothing changed. Re-run with --apply to make the change.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{DB_PATH}.bak-{stamp}-pre-canonical-flip"
    dest = sqlite3.connect(backup_path)
    conn.backup(dest)
    dest.close()
    print(f"Backup written: {backup_path}")

    try:
        conn.execute("BEGIN")
        # unique index allows only one 'yes' at a time: clear first, then set
        conn.execute("UPDATE sources SET is_canonical_actuals='no', updated_at=datetime('now') "
                     "WHERE is_canonical_actuals='yes'")
        conn.execute("UPDATE sources SET is_canonical_actuals='yes', updated_at=datetime('now') "
                     "WHERE name=?", (args.to,))
        after = snapshot(conn)
        yes = [r for r in after if r[2] == "yes"]
        assert len(yes) == 1 and yes[0][1] == args.to, "validation failed: expected exactly one canonical source"
        assert [r[3] for r in after] == [r[3] for r in before], "validation failed: statistics counts changed"
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"ROLLED BACK, nothing changed: {e}")
        return 1

    print_snapshot("AFTER", snapshot(conn))
    print("Done. Validation passed (exactly one canonical source, statistics row counts unchanged).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
