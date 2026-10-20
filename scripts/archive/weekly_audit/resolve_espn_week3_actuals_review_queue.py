"""
Resolves the 7 pending ESPN Week 3 'player_not_found' review items created
by the 2026-09-30 Week 3 actuals pull (3 distinct players, all identity-
confirmed via web search this session; ESPN's own position matches):

  - Jackson Meeks (TE)    -- Detroit Lions TE (WR-to-TE convert, UDFA Syracuse 2025; Lions roster page)
  - Sterling Shepard (WR) -- NY Jets WR (signed to practice squad 2026-09-15)
  - Lew Nichols (RB)      -- Pittsburgh Steelers RB (Lew Nichols III)

Note: Sterling Shepard has 2 *projection*-type rows (Week 3 is locked), so
those are expected to fail with a lock error; the actuals will accept.

Run from the repo root:
    python resolve_espn_week3_actuals_review_queue.py > out_espn_wk3_actuals_review.txt 2>&1
"""
from db.database import get_conn, normalize_name
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"

RESOLUTIONS = {
    "Jackson Meeks":    ("TE", "Jackson Meeks"),
    "Sterling Shepard": ("WR", "Sterling Shepard"),
    "Lew Nichols":      ("RB", "Lew Nichols"),
}


def main():
    conn = get_conn(DB_PATH)

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

    # Verify actuals in the statistics table (source_id=3 is ESPN)
    r = conn.execute(
        """SELECT COUNT(DISTINCT player_id) p, COUNT(*) n FROM statistics
           WHERE source_id=3 AND week_id=(SELECT week_id FROM weeks WHERE season_id=1 AND week_number=3)"""
    ).fetchone()
    print(f"ESPN Week 3 statistics: {r['p']} distinct players, {r['n']} rows")
    conn.close()


if __name__ == "__main__":
    main()
