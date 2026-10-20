"""
One-time remediation: resolves the pending 'player_not_found' review items
left over from the Yahoo Week 2 Player List ingestion (run_yahoo_week2_pull.py).

All 10 are genuinely new players not yet in the roster (no OCR tags/truncation
this time -- the payload was built from a deterministic pdftotext -layout
parse, not manual screenshot transcription, so there's no OCR noise to strip).
Identity confirmed via web search (multiple independent sources: team sites,
ESPN, NFL.com, RotoWire, Wikipedia, Pro-Football-Reference) before creating
anything, per the app's "identity resolution never guesses" invariant.
Position is trusted from Yahoo's own position-column placement.

Run from the repo root:
    python resolve_yahoo_week2_review_queue.py
"""
from db.database import get_conn
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"

# name Yahoo produced -> position (all confirmed real players via web search,
# no truncation/tag stripping needed for any of these)
RESOLUTIONS = {
    "Bauer Sharp":      "TE",   # Tampa Bay Buccaneers TE (2026 6th-round pick, LSU)
    "Brevin Jordan":     "TE",   # Houston Texans TE
    "Britain Covey":     "WR",   # Philadelphia Eagles WR
    "Cameron Latu":       "TE",   # New England Patriots TE
    "Dallen Bentley":      "TE",   # Denver Broncos TE (2026 7th-round pick, Utah)
    "Feleipe Franks":      "TE",   # Carolina Panthers TE
    "Jaret Patterson":     "RB",   # Los Angeles Chargers RB
    "Kylen Granson":       "TE",   # Tennessee Titans TE
    "Michael Carter":       "RB",   # Tennessee Titans RB
    "Robert Tonyan":        "TE",   # Pittsburgh Steelers TE
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
