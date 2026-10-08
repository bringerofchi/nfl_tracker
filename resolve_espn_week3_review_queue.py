"""
Resolves the 16 pending ESPN Week 3 'player_not_found' review items.

All 5 distinct names were already identity-confirmed via web search this
session (2026-09-24) while resolving the parallel Yahoo Week 3 review
queue -- Case Keenum and E.J. Jenkins are the exact same players already
created there. This script just points ESPN's own still-pending
proposed_observations at those same player records (or creates the
remaining 3, identity-confirmed below) rather than re-guessing anything.

  - Case Keenum (QB) -- already exists (created resolving Yahoo Week 3), Chi
  - E.J. Jenkins (TE) -- already exists (created resolving Yahoo Week 3), Phi
  - Hayden Rucci (TE) -- LA Chargers TE (signed 2026 after Chargers put two TEs on IR)
  - J. Michael Sturdivant (WR) -- Green Bay Packers WR (made 2026 53-man roster, UCLA)
  - Mark Redman (TE) -- Green Bay Packers TE (traded from Rams to Packers, Aug 2026)

Run from the repo root:
    python resolve_espn_week3_review_queue.py
"""
from db.database import get_conn, normalize_name
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"

# raw name ESPN produced -> (position, canonical_display_name)
RESOLUTIONS = {
    "Case Keenum":            ("QB", "Case Keenum"),
    "E.J. Jenkins":           ("TE", "E.J. Jenkins"),
    "Hayden Rucci":           ("TE", "Hayden Rucci"),
    "J. Michael Sturdivant":  ("WR", "J. Michael Sturdivant"),
    "Mark Redman":            ("TE", "Mark Redman"),
}


def main():
    conn = get_conn(DB_PATH)

    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position, ri.reason
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
    created, reused, aliased = 0, 0, 0
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

        if normalize_name(raw_name) != normalize_name(canonical):
            norm_alias = normalize_name(raw_name)
            already = conn.execute(
                "SELECT 1 FROM player_name_aliases WHERE player_id=? AND normalized_alias=?",
                (player_id, norm_alias),
            ).fetchone()
            if not already:
                conn.execute(
                    """INSERT INTO player_name_aliases (player_id, alias, normalized_alias, alias_source)
                       VALUES (?, ?, ?, 'claude_review_queue_fix')""",
                    (player_id, raw_name, norm_alias),
                )
                aliased += 1
    conn.commit()
    print(f"Players: {created} created, {reused} matched to an existing player, {aliased} aliases added.")
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
    print(f"Remaining pending review items (all sources): {remaining}")
    conn.close()


if __name__ == "__main__":
    main()
