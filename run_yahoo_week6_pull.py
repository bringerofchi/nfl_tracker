"""
Yahoo Week 6, 2026 weekly Player List pull -- reads the real specimen
(15 PDF exports of Yahoo's Player List, Stats filter = "Week 6 (proj)",
top 374 offensive players by Fan Pts), pre-parsed into
uploads/yahoo_week6_playerlist.json by a deterministic pdftotext -layout
parser (column positions are fixed-width per the page's own layout, not
OCR/vision guesswork), through YahooWeeklyPdfAdapter and writes into the
REAL db/tracker.db through the actual ingest_collector_batch pipeline --
same path every other source uses.

Parser note: 374 rows parsed (15 pages x 25, last page 24), 0 discarded.
The page-break glitch check from Week 3 was applied to the last two rows of
each printed page (no glitched rows found this week). Two mid-page rows
(Jaleel McLaughlin rush_yards=17, Xavier Restrepo receptions=1) were
rendered by Yahoo as whole numbers with no ".0" and were accepted as-is.

This run reuses YahooWeeklyPdfAdapter unchanged from Week 2 -- see its
docstring for how Pre-Season vs Actual rank map onto the existing
scope='season'/'weekly' dimension.

Run from the repo root:
    python run_yahoo_week6_pull.py
"""
import sys

from db.database import get_conn
from ingestion import pipeline
from ingestion.yahoo_weekly_adapter import YahooWeeklyPdfAdapter
from ingestion.collector import AdapterUnavailable

SEASON_YEAR = 2026
WEEK_NUMBER = 6
PAYLOAD_PATH = "uploads/yahoo_week6_playerlist.json"


def main():
    conn = get_conn("db/tracker.db")
    season_row = conn.execute("SELECT season_id FROM seasons WHERE year=?", (SEASON_YEAR,)).fetchone()
    if not season_row:
        print(f"[FAIL] No season row found for year={SEASON_YEAR} in db/tracker.db.")
        sys.exit(1)
    season_id = season_row["season_id"]

    adapter = YahooWeeklyPdfAdapter(source_name="Yahoo", payload_path=PAYLOAD_PATH)

    print(f"Pulling Yahoo weekly Player List data: season={SEASON_YEAR} (season_id={season_id}), week={WEEK_NUMBER}...")
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
        print("First 30 errors:")
        for e in result["errors"][:30]:
            print(" ", e)

    print()
    print("=== Totals landed this run (projections, week 6, source=Yahoo) ===")
    total = conn.execute("""
        SELECT COUNT(DISTINCT player_id) AS players, COUNT(*) AS rows
        FROM projections
        WHERE source_name='Yahoo' AND scope='weekly' AND week_id=(
            SELECT week_id FROM weeks WHERE season_id=? AND week_number=?
        )
    """, (season_id, WEEK_NUMBER)).fetchone()
    print(f"  distinct players: {total['players']}, total rows: {total['rows']}")

    print()
    print("=== Totals landed this run (rankings, source=Yahoo) ===")
    rk = conn.execute("""
        SELECT r.scope, COUNT(DISTINCT r.player_id) AS players, COUNT(*) AS rows
        FROM rankings r JOIN sources s ON s.source_id = r.source_id
        WHERE s.name='Yahoo' AND r.ranking_type='overall'
        GROUP BY r.scope
    """).fetchall()
    for row in rk:
        print(f"  scope={row['scope']}: distinct players={row['players']}, rows={row['rows']}")

    conn.close()
    print()
    print("Done. Paste this output back to Claude.")


if __name__ == "__main__":
    main()
