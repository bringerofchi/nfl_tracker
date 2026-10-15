"""
Resolves the Week 6 (2026-10-15) pending review queue after the Yahoo pull
(ESPN was resolved earlier by resolve_week6_review_queue.py; Athletic had 0).

Part 1 -- 30 'proposed_observation'/'player_not_found' rows, 7 distinct new
players, all identity-confirmed via web search 2026-10-15 (never guessed);
the team shown matches Yahoo's own team tag except where noted:
  - Layne Pryor (TE)       -- Houston Texans (signed future deal; Rotowire)
  - Mitch Van Vooren (TE)  -- New England Patriots signed him 7/22, released
                              8/18 (patriots.com / FantasyNerds); Yahoo still
                              lists him NE, NA tag, ranks only, no projections
  - Patrick Herbert (TE)   -- Los Angeles Chargers practice squad (signed 9/1)
  - Pharaoh Brown (TE)     -- Indianapolis Colts (promoted to 53-man; colts.com)
  - Stone Smartt (TE)      -- New Orleans Saints practice squad (9/1 moves)
  - Xavier Restrepo (WR)   -- Tennessee Titans (Miami OH UDFA; Titans site)
  - Zavier Scott (RB)      -- Chicago Bears, elevated from practice squad
Players are created only if resolve_player finds no match; any non-exact,
non-none match is skipped for a human look.

Part 2 -- pending 'ranking'/'conflict' items (349, Yahoo): Justin's standing
decision (2026-09-24) is that a pre-season overall rank is never changed once
set. Each conflict whose NEW ranking is scope='season' is rejected through
repo.resolve_ranking_conflict(action='rejected') (logged in `corrections`).
Anything that is NOT a season-scope conflict is left untouched and printed.

Run from the repo root:
    python resolve_yahoo_week6_review_queue.py > out_resolve_yahoo_wk6.txt 2>&1
"""
import json

from db.database import get_conn, normalize_name
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"

# raw name -> (position, canonical_display_name)
RESOLUTIONS = {
    "Layne Pryor":      ("TE", "Layne Pryor"),
    "Mitch Van Vooren": ("TE", "Mitch Van Vooren"),
    "Patrick Herbert":  ("TE", "Patrick Herbert"),
    "Pharaoh Brown":    ("TE", "Pharaoh Brown"),
    "Stone Smartt":     ("TE", "Stone Smartt"),
    "Xavier Restrepo":  ("WR", "Xavier Restrepo"),
    "Zavier Scott":     ("RB", "Zavier Scott"),
}


def main():
    conn = get_conn(DB_PATH)

    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position, po.source_name
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND ri.reason='player_not_found'"""
    ).fetchall()
    print(f"Pending player_not_found rows: {len(pending)}")

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
        print(f"  {canonical} ({position}) -> player_id={player_id} ({match_type})")
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
            print(f"[FAIL] proposed_id={row['proposed_id']} ({row['extracted_player_name']}, {row['source_name']}): {e}")
    print(f"Accepted {accepted} observations, {failed} failed.")
    print()

    # ---- Part 2: reject frozen pre-season rank conflicts ----
    conflicts = conn.execute(
        "SELECT review_id, details_json FROM review_items WHERE status='pending' AND entity_type='ranking' AND reason='conflict'"
    ).fetchall()
    print(f"Pending ranking conflicts: {len(conflicts)}")
    rejected, skipped, rfail = 0, 0, 0
    for c in conflicts:
        d = json.loads(c["details_json"])
        new_row = conn.execute("SELECT scope FROM rankings WHERE ranking_id=?", (d["new_ranking_id"],)).fetchone()
        scope = new_row["scope"] if new_row else None
        if scope != "season":
            skipped += 1
            print(f"[SKIP] review_id={c['review_id']} scope={scope} details={c['details_json']}")
            continue
        try:
            repo.resolve_ranking_conflict(conn, c["review_id"], "rejected", resolved_by="justin_standing_decision")
            rejected += 1
        except ValueError as e:
            rfail += 1
            print(f"[FAIL] review_id={c['review_id']}: {e}")
    conn.commit()
    print(f"Rejected {rejected}, skipped (not season-scope) {skipped}, failed {rfail}.")

    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    print(f"Remaining pending review items (all sources): {remaining}")
    conn.close()


if __name__ == "__main__":
    main()
