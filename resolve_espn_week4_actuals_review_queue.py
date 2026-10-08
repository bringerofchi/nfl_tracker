"""
Resolves the 14 pending ESPN Week 4 'player_not_found' review items created
by the 2026-10-07 Week 4 actuals pull (3 distinct players, identity confirmed
via web search; ESPN's own position matches):

  - Athan Kaliakmanis (QB) -- Washington Commanders rookie QB (7th round, #223, 2026 draft; Commanders.com)
  - Craig Reynolds (RB)    -- veteran RB, released by Washington 2026-08-30 (NBC Sports)
  - Velus Jones Jr. (RB)   -- Seattle Seahawks RB, promoted to the active roster 2026-10-03 (Rotowire)

Note: Velus Jones Jr. has only *projection*-type rows pending (Week 4 is locked),
so those are expected to fail with a lock error; the player is still created so
the queue clears and future weeks resolve automatically.

Also sets Week 4's weeks.status to 'completed' (final stats are in) and prints
every week's status before/after so any drift in earlier weeks is visible.

Run from the repo root:
    python resolve_espn_week4_actuals_review_queue.py > out_espn_wk4_actuals_review.txt 2>&1
"""
from db.database import get_conn
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"
SEASON_YEAR = 2026
WEEK_NUMBER = 4

RESOLUTIONS = {
    "Athan Kaliakmanis": ("QB", "Athan Kaliakmanis"),
    "Craig Reynolds":    ("RB", "Craig Reynolds"),
    "Velus Jones Jr.":   ("RB", "Velus Jones Jr."),
}


def print_week_statuses(conn, season_id, label):
    print(label)
    for r in conn.execute(
        "SELECT week_number, status FROM weeks WHERE season_id=? ORDER BY week_number", (season_id,)
    ).fetchall():
        print(f"  week {r['week_number']}: {r['status']}")


def main():
    conn = get_conn(DB_PATH)
    season_id = conn.execute("SELECT season_id FROM seasons WHERE year=?", (SEASON_YEAR,)).fetchone()["season_id"]

    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.data_type, po.stat_name
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND po.source_name='ESPN' AND ri.reason='player_not_found'"""
    ).fetchall()

    unmapped = sorted({r["extracted_player_name"] for r in pending} - set(RESOLUTIONS))
    if unmapped:
        print("[FAIL] These pending names have no resolution mapped -- aborting, nothing written:")
        for n in unmapped:
            print("  ", n)
        conn.close()
        return

    resolved_ids = {}
    created, reused = 0, 0
    for raw_name, (position, canonical) in RESOLUTIONS.items():
        existing_id, match_type = repo.resolve_player(conn, canonical, position)
        if match_type == "exact":
            player_id = existing_id
            reused += 1
        elif match_type == "none":
            player_id = repo.create_player(conn, canonical, position)
            created += 1
        else:
            print(f"[SKIP] '{canonical}' ({position}) matched as '{match_type}' -- needs a human look, not touching.")
            continue
        resolved_ids[raw_name] = player_id
    conn.commit()
    print(f"Players: {created} created, {reused} matched to an existing player.")
    print()

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
            print(f"[FAIL] proposed_id={row['proposed_id']} ({row['extracted_player_name']} {row['stat_name']} {row['data_type']}): {e}")

    print(f"Accepted {accepted} observations, {failed} failed.")
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

    print()
    print_week_statuses(conn, season_id, "Week statuses BEFORE:")
    conn.execute(
        "UPDATE weeks SET status='completed' WHERE season_id=? AND week_number=? AND week_type='regular'",
        (season_id, WEEK_NUMBER),
    )
    conn.commit()
    print_week_statuses(conn, season_id, "Week statuses AFTER:")
    conn.close()


if __name__ == "__main__":
    main()
