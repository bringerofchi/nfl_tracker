"""
Resolves the 8 pending ESPN Week 6 review items created by the 2026-10-20
Week 6 actuals pull (re-run of run_espn_week6_pull.py after games finished).

Part 1 -- 7 'player_not_found' rows, 2 distinct new players. Identities
confirmed 2026-10-20 (never guessed); ESPN's stat rows match where checkable:

  - Andrew Armstrong (WR) -- Kansas City Chiefs practice squad. Ourlads (WR,
                             Chiefs PS; elevated to the active roster in Oct
                             2026, then back to PS) + Chiefs/atozsports roster
                             coverage (signed Jan 2026, cut Aug, re-signed PS).
                             2 ACTUAL rows (receptions=1 ...).
  - Will Mallory (TE)     -- Indianapolis Colts, TE #86. colts.com game log:
                             Week 6 vs TEN = 2 rec / 20 yds -- matches ESPN's
                             2 receptions. Ourlads Colts depth chart + Wikipedia
                             agree. 2 ACTUAL rows + 3 PROJECTION rows (the
                             projection rows land as late-after-lock, per the
                             2026-10-07 rule; excluded from accuracy comparisons).

Part 2 -- 1 'invalid_value' row: Cade Otton (TE) receiving_tds projection of
-0.002995605, re-proposed by this pull. The same value was already accepted as
0.0 on the first Week 6 pull (resolve_week6_review_queue.py). Accepting it
again would only add a duplicate frozen row, so: if an accepted Week 6 ESPN
projection for Otton receiving_tds exists, the duplicate is REJECTED (audit
trail kept, nothing deleted); otherwise it is accepted with corrected_value=0.0.

Safety rails (per your git/DB hygiene rules):
  - DRY RUN by default: reads only, prints what it WOULD do, writes nothing.
  - --apply: copies db/tracker.db to db/tracker.db.bak-<date>-pre-wk6-actuals
    first, then creates players and resolves the items.
  - Players are created only if resolve_player returns match 'none'; any
    non-exact, non-none match is skipped for a human look.
  - Does NOT touch weeks.status (display-only; "closed" == locks).

Run from anywhere:
    python scripts/resolve_espn_week6_actuals_review_queue.py > out_wk6_actuals_dry.txt 2>&1
    python scripts/resolve_espn_week6_actuals_review_queue.py --apply > out_wk6_actuals_apply.txt 2>&1
"""
import datetime
import os
import shutil
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from db.database import get_conn  # noqa: E402
from db import repository as repo  # noqa: E402
from ingestion import pipeline  # noqa: E402

DB_PATH = os.path.join(REPO_ROOT, "db", "tracker.db")
SEASON_YEAR = 2026
WEEK_NUMBER = 6

# raw name -> (position, canonical_display_name)
RESOLUTIONS = {
    "Andrew Armstrong": ("WR", "Andrew Armstrong"),
    "Will Mallory":     ("TE", "Will Mallory"),
}

NEGLIGIBLE_NEGATIVE = -0.01  # only treat values between this and 0 as rounding noise


def main():
    apply = "--apply" in sys.argv
    print(f"MODE: {'APPLY' if apply else 'DRY RUN (nothing will be written)'}")
    print()

    conn = get_conn(DB_PATH)
    season_id = conn.execute("SELECT season_id FROM seasons WHERE year=?", (SEASON_YEAR,)).fetchone()["season_id"]
    week_id = conn.execute(
        "SELECT week_id FROM weeks WHERE season_id=? AND week_number=?", (season_id, WEEK_NUMBER)
    ).fetchone()["week_id"]

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

    invalid = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.stat_name, po.stat_value, po.resolved_player_id
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND po.source_name='ESPN' AND ri.reason='invalid_value'"""
    ).fetchall()
    print()
    print(f"Pending ESPN invalid_value rows: {len(invalid)}")
    inv_plan = []
    for r in invalid:
        v = r["stat_value"]
        if v is None or not (NEGLIGIBLE_NEGATIVE < v < 0) or r["resolved_player_id"] is None:
            print(f"  [SKIP] proposed_id={r['proposed_id']} {r['extracted_player_name']} {r['stat_name']}={v} "
                  f"-- not a negligible negative on a resolved player, needs a human look.")
            continue
        existing = conn.execute(
            """SELECT projected_value FROM projections
               WHERE player_id=? AND source_name='ESPN' AND scope='weekly' AND week_id=? AND stat_name=?""",
            (r["resolved_player_id"], week_id, r["stat_name"]),
        ).fetchall()
        if existing:
            print(f"  proposed_id={r['proposed_id']} {r['extracted_player_name']} {r['stat_name']}={v}: "
                  f"accepted row already on file ({[e['projected_value'] for e in existing]}) -> would REJECT duplicate")
            inv_plan.append((r["proposed_id"], "reject"))
        else:
            print(f"  proposed_id={r['proposed_id']} {r['extracted_player_name']} {r['stat_name']}={v}: "
                  f"nothing on file -> would ACCEPT with corrected_value=0.0")
            inv_plan.append((r["proposed_id"], "accept0"))

    if not apply:
        print()
        print("Dry run complete. Re-run with --apply to write.")
        conn.close()
        return

    backup = f"{DB_PATH}.bak-{datetime.date.today():%Y%m%d}-pre-wk6-actuals"
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
    print(f"Accepted {accepted} observations, {failed} failed.")

    for pid, action in inv_plan:
        try:
            if action == "reject":
                pipeline.reject_proposed_observation(
                    conn, pid, reason="duplicate of already-accepted Week 6 projection (negligible negative, accepted as 0.0)")
                print(f"  rejected duplicate proposed_id={pid}")
            else:
                pipeline.accept_proposed_observation(conn, pid, corrected_value=0.0)
                print(f"  accepted proposed_id={pid} with corrected_value=0.0")
        except ValueError as e:
            print(f"[FAIL] proposed_id={pid}: {e}")

    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    print(f"Remaining pending review items (all sources): {remaining}")
    r = conn.execute(
        """SELECT COUNT(DISTINCT s.player_id) p, COUNT(*) n
           FROM statistics s JOIN sources src ON src.source_id = s.source_id
           WHERE src.name='ESPN' AND s.week_id=?""",
        (week_id,),
    ).fetchone()
    print(f"ESPN Week {WEEK_NUMBER} statistics: {r['p']} distinct players, {r['n']} rows")
    conn.close()


if __name__ == "__main__":
    main()
