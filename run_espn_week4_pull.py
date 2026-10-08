"""
Real Week 4, 2026 ESPN weekly pull -- live network call, full player
population, writes into the REAL db/tracker.db through the actual
ingest_collector_batch pipeline (same path the scheduler/UI would use).

This is NOT a verification/throwaway-DB script -- it writes real data
into your real database, same as the live app would. Run this once
Week 4's ESPN data is actually available (projections should be
available now; actuals will only populate once games are played).

Run from the repo root:
    python run_espn_week4_pull.py
"""
import sys

from db.database import get_conn
from ingestion import pipeline
from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter
from ingestion.collector import AdapterUnavailable

SEASON_YEAR = 2026
WEEK_NUMBER = 4


def main():
    conn = get_conn("db/tracker.db")
    season_row = conn.execute("SELECT season_id FROM seasons WHERE year=?", (SEASON_YEAR,)).fetchone()
    if not season_row:
        print(f"[FAIL] No season row found for year={SEASON_YEAR} in db/tracker.db.")
        sys.exit(1)
    season_id = season_row["season_id"]

    # No player_ids filter -- full population, same limit/sort strategy
    # already validated in FINDINGS.md section E (avoids the endpoint's
    # silent 50-player default truncation). season_year is passed
    # explicitly and separately from season_id -- season_id (the DB's
    # internal seasons.season_id, e.g. 1) is NOT the same thing as the
    # real NFL year ESPN's URL needs (e.g. 2026); conflating the two
    # produced a real 404 the first time this ran. See
    # ESPNWeeklyLiveAdapter's docstring for the full explanation.
    adapter = ESPNWeeklyLiveAdapter(source_name="ESPN", season_year=SEASON_YEAR)

    print(f"Pulling real ESPN weekly data: season={SEASON_YEAR} (season_id={season_id}), week={WEEK_NUMBER}...")
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

    print()
    print(f"auto_accepted:     {len(result['auto_accepted'])}")
    print(f"sent_to_review:    {len(result['sent_to_review'])}")
    print(f"duplicates_skipped:{len(result['duplicates_skipped'])}")
    print(f"errors:            {len(result['errors'])}")
    if result["errors"]:
        print()
        print("First 10 errors:")
        for e in result["errors"][:10]:
            print(" ", e)

    print()
    print("=== Sample of what landed (projections, Week 4, source=ESPN) ===")
    rows = conn.execute("""
        SELECT p.display_name, pr.stat_name, pr.projected_value
        FROM projections pr JOIN players p ON p.player_id = pr.player_id
        WHERE pr.source_name='ESPN' AND pr.scope='weekly' AND pr.week_id=(
            SELECT week_id FROM weeks WHERE season_id=? AND week_number=?
        )
        LIMIT 15
    """, (season_id, WEEK_NUMBER)).fetchall()
    for r in rows:
        print(" ", dict(r))

    print()
    print("=== Totals landed this run (projections, Week 4, source=ESPN) ===")
    total = conn.execute("""
        SELECT COUNT(DISTINCT player_id) AS players, COUNT(*) AS rows
        FROM projections
        WHERE source_name='ESPN' AND scope='weekly' AND week_id=(
            SELECT week_id FROM weeks WHERE season_id=? AND week_number=?
        )
    """, (season_id, WEEK_NUMBER)).fetchone()
    print(f"  distinct players: {total['players']}, total rows: {total['rows']}")

    conn.close()
    print()
    print("Done. Paste this output back to Claude.")


if __name__ == "__main__":
    main()
