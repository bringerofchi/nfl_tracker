"""
The Athletic Week 3, 2026 weekly projections xlsx pull -- reads the real
specimen file (Week_3_2026_Proj.xlsx) through AthleticWeeklyXlsxAdapter and
writes into the REAL db/tracker.db through the actual
ingest_collector_batch pipeline (same path the scheduler/UI would use for
any other source).

Run from the repo root:
    python run_athletic_week3_pull.py
"""
import sys

from db.database import get_conn
from ingestion import pipeline
from ingestion.athletic_weekly_xlsx_adapter import AthleticWeeklyXlsxAdapter
from ingestion.collector import AdapterUnavailable

SEASON_YEAR = 2026
WEEK_NUMBER = 3
FILE_PATH = "uploads/Week_3_2026_Proj.xlsx"


def main():
    conn = get_conn("db/tracker.db")
    season_row = conn.execute("SELECT season_id FROM seasons WHERE year=?", (SEASON_YEAR,)).fetchone()
    if not season_row:
        print(f"[FAIL] No season row found for year={SEASON_YEAR} in db/tracker.db.")
        sys.exit(1)
    season_id = season_row["season_id"]

    adapter = AthleticWeeklyXlsxAdapter(source_name="The Athletic", file_path=FILE_PATH)

    print(f"Pulling The Athletic weekly data: season={SEASON_YEAR} (season_id={season_id}), week={WEEK_NUMBER}...")
    try:
        result = pipeline.ingest_collector_batch(conn, adapter, season_id, week_number=WEEK_NUMBER)
    except AdapterUnavailable as e:
        print(f"[FAIL] AdapterUnavailable: {e} (transient={e.transient})")
        conn.close()
        sys.exit(1)

    if result.get("fetch_failed"):
        print(f"[FAIL] fetch_failed: {result.get('error')}")
        conn.close()
        sys.exit(1)
    if result.get("run_locked"):
        print("[FAIL] run_locked: a collection run for this source/season/week is already in progress.")
        conn.close()
        sys.exit(1)

    print()
    print(f"auto_accepted:      {len(result['auto_accepted'])}")
    print(f"sent_to_review:     {len(result['sent_to_review'])}")
    print(f"duplicates_skipped: {len(result['duplicates_skipped'])}")
    print(f"errors:             {len(result['errors'])}")
    if result["errors"]:
        print()
        print("First 15 errors:")
        for e in result["errors"][:15]:
            print(" ", e)

    print()
    print("=== Sample of what landed (projections, week 3, source=The Athletic) ===")
    rows = conn.execute("""
        SELECT p.display_name, pr.stat_name, pr.projected_value
        FROM projections pr JOIN players p ON p.player_id = pr.player_id
        WHERE pr.source_name='The Athletic' AND pr.scope='weekly' AND pr.week_id=(
            SELECT week_id FROM weeks WHERE season_id=? AND week_number=?
        )
        LIMIT 15
    """, (season_id, WEEK_NUMBER)).fetchall()
    for r in rows:
        print(" ", dict(r))

    print()
    print("=== Totals landed this run (projections, week 3, source=The Athletic) ===")
    total = conn.execute("""
        SELECT COUNT(DISTINCT player_id) AS players, COUNT(*) AS rows
        FROM projections
        WHERE source_name='The Athletic' AND scope='weekly' AND week_id=(
            SELECT week_id FROM weeks WHERE season_id=? AND week_number=?
        )
    """, (season_id, WEEK_NUMBER)).fetchone()
    print(f"  distinct players: {total['players']}, total rows: {total['rows']}")

    conn.close()
    print()
    print("Done.")


if __name__ == "__main__":
    main()
