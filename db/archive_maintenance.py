"""
CLI for archiving resolved pipeline/audit-trail residue out of tracker.db
into db/archive.db. See db/archive.py module docstring for the eligibility
rules -- resolved is necessary but never sufficient.

Usage (run from the repo root):

    python db/archive_maintenance.py --report
        Read-only. Opens tracker.db, computes exactly which rows are
        currently eligible, prints counts/bytes. Makes NO writes to
        tracker.db or archive.db (archive.db is created with an empty
        schema if it doesn't exist yet, but nothing is written into it).

    python db/archive_maintenance.py --apply --table raw_observations
        Actually archives (copy -> verify -> delete) the eligible rows for
        ONE table. --table is required for --apply -- there is no
        "--apply everything" in one shot, so each table's effect on the
        live app can be checked before moving to the next one. This
        matches the phased rollout (raw_observations first, then
        proposed_observations, then review_items).

    python db/archive_maintenance.py --apply --table raw_observations --dry-run
        Runs the full copy+verify path and writes a manifest row, but never
        deletes from tracker.db. Useful to sanity-check byte estimates
        against the real report before the first live --apply.

Flags:
    --dwell-days N      default 14 (see db/archive.py DWELL_DAYS_DEFAULT)
    --live-db PATH      default db/tracker.db
    --archive-db PATH   default db/archive.db
"""
import argparse
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import archive as arc


def _live_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", action="store_true", help="Read-only eligibility report. Default mode.")
    p.add_argument("--apply", action="store_true", help="Actually archive eligible rows for --table.")
    p.add_argument("--table", choices=["raw_observations", "proposed_observations", "review_items"],
                   help="Required with --apply. One table per run.")
    p.add_argument("--dry-run", action="store_true",
                   help="With --apply: run copy+verify and write a manifest row, but never delete from tracker.db.")
    p.add_argument("--dwell-days", type=int, default=arc.DWELL_DAYS_DEFAULT)
    p.add_argument("--live-db", default=os.path.join(os.path.dirname(__file__), "tracker.db"))
    p.add_argument("--archive-db", default=os.path.join(os.path.dirname(__file__), "archive.db"))
    args = p.parse_args()

    if args.apply and not args.table:
        p.error("--apply requires --table")

    live_conn = _live_conn(args.live_db)

    if args.apply:
        archive_conn = arc.get_archive_conn(args.archive_db)
        report = arc.build_report(live_conn, dwell_days=args.dwell_days)
        ids = report["eligible_ids"][args.table]
        if not ids:
            print(f"No eligible rows in {args.table} right now. Nothing to do.")
            return
        print(f"About to archive {len(ids)} row(s) from {args.table} "
              f"({'DRY RUN -- no deletion from tracker.db' if args.dry_run else 'live -- deletes from tracker.db'})")
        moved, bytes_est = arc.apply_archive(live_conn, archive_conn, args.table, ids, dry_run=args.dry_run)
        print(f"Moved {moved} row(s), ~{bytes_est/1024/1024:.2f} MB, into {args.archive_db}")
        if not args.dry_run:
            print(f"Deleted {moved} row(s) from {args.table} in {args.live_db}")
        print("Run VACUUM on tracker.db separately to actually shrink the file on disk "
              "(deleting rows frees pages internally but SQLite doesn't shrink the file "
              "without an explicit VACUUM).")
        return

    # Default / --report
    report = arc.build_report(live_conn, dwell_days=args.dwell_days)
    arc.print_report(report)


if __name__ == "__main__":
    main()
