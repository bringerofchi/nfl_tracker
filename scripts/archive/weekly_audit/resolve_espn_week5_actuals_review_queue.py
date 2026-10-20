"""
Resolves the 9 pending ESPN Week 5 'player_not_found' review items created by
the 2026-10-14 Week 5 pull (4 distinct players). Each identity was confirmed
from TWO independent sources on 2026-10-14 (never guessed); ESPN's own stat
rows are consistent with the sources where checkable:

  - Simi Fehoko (WR)       -- Arizona Cardinals. Rotowire (re-signed 3/13/2026,
                              1-yr deal; 8/7/2026 preseason TD) + CBS Sports
                              player page (WR #80, Cardinals).
                              2 PROJECTION rows pending -> expected to fail the
                              Week 5 lock; player is still created.
  - Teagan Quitoriano (TE) -- Arizona Cardinals. Rotowire (promoted from
                              practice squad 10/6/2026; 10/11: 2 targets, 15 yds,
                              2 TD vs DET -- matches ESPN's 2 rec/15 yd/2 TD) +
                              Wikipedia (TE #47, active roster 10/6/2026).
  - Deven Thompkins (WR)   -- Las Vegas Raiders (practice squad; elevated for
                              Week 5). Rotowire (10/12/2026: 1 target, 8 yds in
                              Week 5 -- matches ESPN's 1 rec/8 yd) + raiders.com
                              (WR #83, signed 7/27/2026).
  - Kalel Mullings (RB)    -- Tennessee Titans (practice squad; elevated 10/10).
                              Rotowire (10/10 elevation, 10/12 back to PS) +
                              tennesseetitans.com (RB; Week 5 vs HOU: 1 carry,
                              3 yds -- matches ESPN's 1 att/3 yd).

Safety rails (per your git/DB hygiene rules):
  - DRY RUN by default: reads only, prints what it WOULD do, writes nothing.
  - --apply: copies db/tracker.db to db/tracker.db.bak-<date>-pre-wk5-actuals
    first, then creates players and accepts the proposed observations.
  - Players are created only if resolve_player returns match 'none'; any
    non-exact, non-none match is skipped for a human look.
  - Does NOT touch weeks.status (display-only column; "closed" == locks).

Run from the repo root:
    python resolve_espn_week5_actuals_review_queue.py > out_wk5_actuals_dry.txt 2>&1
    python resolve_espn_week5_actuals_review_queue.py --apply > out_wk5_actuals_apply.txt 2>&1
"""
import shutil
import sys
import datetime

from db.database import get_conn
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"
SEASON_YEAR = 2026
WEEK_NUMBER = 5

# raw name -> (position, canonical_display_name)
RESOLUTIONS = {
    "Simi Fehoko":       ("WR", "Simi Fehoko"),
    "Teagan Quitoriano": ("TE", "Teagan Quitoriano"),
    "Deven Thompkins":   ("WR", "Deven Thompkins"),
    "Kalel Mullings":    ("RB", "Kalel Mullings"),
}


def main():
    apply = "--apply" in sys.argv
    print(f"MODE: {'APPLY' if apply else 'DRY RUN (nothing will be written)'}")
    print()

    conn = get_conn(DB_PATH)
    season_id = conn.execute("SELECT season_id FROM seasons WHERE year=?", (SEASON_YEAR,)).fetchone()["season_id"]

    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.data_type, po.stat_name
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND po.source_name='ESPN' AND ri.reason='player_not_found'"""
    ).fetchall()
    print(f"Pending ESPN player_not_found rows: {len(pending)}")

    unmapped = sorted({r["extracted_player_name"] for r in pending} - set(RESOLUTIONS))
    if unmapped:
        print("[FAIL] These pending names have no resolution mapped -- aborting, nothing written:")
        for n in unmapped:
            print("  ", n)
        conn.close()
        return

    plan = {}
    for raw_name, (position, canonical) in RESOLUTIONS.items():
        existing_id, match_type = repo.resolve_player(conn, canonical, position)
        plan[raw_name] = (existing_id, match_type, position, canonical)
        print(f"  {canonical} ({position}): resolve_player -> match_type={match_type}, id={existing_id}")
    print()

    for r in pending:
        print(f"  would accept proposed_id={r['proposed_id']}: {r['extracted_player_name']} "
              f"{r['stat_name']} [{r['data_type']}]")

    if not apply:
        print()
        print("Dry run complete. Re-run with --apply to write.")
        conn.close()
        return

    backup = f"{DB_PATH}.bak-{datetime.date.today():%Y%m%d}-pre-wk5-actuals"
    shutil.copy2(DB_PATH, backup)
    print()
    print(f"Backup written: {backup}")

    resolved_ids = {}
    created, reused = 0, 0
    for raw_name, (existing_id, match_type, position, canonical) in plan.items():
        if match_type == "exact":
            resolved_ids[raw_name] = existing_id
            reused += 1
        elif match_type == "none":
            resolved_ids[raw_name] = repo.create_player(conn, canonical, position)
            created += 1
        else:
            print(f"[SKIP] '{canonical}' ({position}) matched as '{match_type}' -- needs a human look, not touching.")
    conn.commit()
    print(f"Players: {created} created, {reused} matched to an existing player.")

    accepted, failed = 0, 0
    for row in pending:
        player_id = resolved_ids.get(row["extracted_player_name"])
        if player_id is None:
            continue
        try:
            pipeline.accept_proposed_observation(conn, row["proposed_id"], player_id_override=player_id)
            accepted += 1
        except ValueError as e:
            failed += 1
            print(f"[FAIL] proposed_id={row['proposed_id']} ({row['extracted_player_name']} "
                  f"{row['stat_name']} {row['data_type']}): {e}")
    print(f"Accepted {accepted} observations, {failed} failed "
          f"(Fehoko's 2 projection rows are expected to fail on the Week 5 lock).")

    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    print(f"Remaining pending review items (all sources): {remaining}")
    r = conn.execute(
        """SELECT COUNT(DISTINCT s.player_id) p, COUNT(*) n
           FROM statistics s JOIN sources src ON src.source_id = s.source_id
           WHERE src.name='ESPN'
             AND s.week_id=(SELECT week_id FROM weeks WHERE season_id=? AND week_number=?)""",
        (season_id, WEEK_NUMBER),
    ).fetchone()
    print(f"ESPN Week {WEEK_NUMBER} statistics: {r['p']} distinct players, {r['n']} rows")
    conn.close()


if __name__ == "__main__":
    main()
