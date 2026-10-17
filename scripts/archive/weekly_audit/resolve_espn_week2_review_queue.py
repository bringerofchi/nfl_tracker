"""
One-time remediation: resolves the pending 'player_not_found' review items
left over from the ESPN Week 2 actuals pull (run_espn_week2_pull.py).

All 7 are genuinely new players missing from the roster -- deep-roster
players, a Week 2 elevation, or a 2026 rookie. Identity confirmed via web
search (multiple independent sources: team sites, ESPN, Wikipedia,
Pro-Football-Reference, RotoWire) before creating anything, per the app's
"identity resolution never guesses" invariant.

Run from the repo root:
    python resolve_espn_week2_review_queue.py
"""
from db.database import get_conn
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"

# name ESPN produced -> position (all confirmed real, currently-active
# players via web search)
RESOLUTIONS = {
    "DJ Herman":        "RB",  # Miami Dolphins RB/FB (2026 rookie, made 53-man roster)
    "Cody White":        "WR",  # Seattle Seahawks WR
    "Travis Homer":       "RB",  # Pittsburgh Steelers RB (elevated for Week 2)
    "KhaDarel Hodge":      "WR",  # San Francisco 49ers WR (added to active roster 9/16/2026)
    "Jack Strand":          "QB",  # Atlanta Falcons QB (2026 rookie, made NFL debut)
    "Colson Yankoff":        "TE",  # Washington Commanders TE
    "Chris Blair":            "WR",  # Atlanta Falcons WR
}


def main():
    conn = get_conn(DB_PATH)

    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position, ri.review_id, po.source_name
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND ri.reason='player_not_found'"""
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

    for raw_name, position in RESOLUTIONS.items():
        existing_id, match_type = repo.resolve_player(conn, raw_name, position)
        if match_type == "exact":
            player_id = existing_id
            reused += 1
        elif match_type == "none":
            player_id = repo.create_player(conn, raw_name, position)
            created += 1
        else:
            print(f"[SKIP] '{raw_name}' ({position}) matched as '{match_type}' -- needs a human look, not touching.")
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
            print(f"[FAIL] proposed_id={row['proposed_id']} ({row['extracted_player_name']}): {e}")

    print(f"Accepted {accepted} observations, {failed} failed.")

    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    total_players = conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    print(f"Remaining pending review items: {remaining}")
    print(f"Total players now in roster: {total_players}")
    conn.close()


if __name__ == "__main__":
    main()
