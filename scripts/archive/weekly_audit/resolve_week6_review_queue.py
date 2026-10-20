"""
Resolves the Week 6 (2026-10-15) ESPN pending review queue (10 items).
Athletic had 0 review items. Yahoo Week 6 is handled separately.

Part 1 -- 9 'proposed_observation'/'player_not_found' rows, 3 distinct new
players, identity-confirmed via web search 2026-10-15 (never guessed):
  - Brandon Smith (WR)   -- Pittsburgh Steelers practice squad, elevated for
                            Week 5 (Rotowire, 10/12/2026)
  - Carson Steele (RB)   -- Philadelphia Eagles (Rotowire: signed Jan 2026,
                            hybrid FB/RB; Eagles active-roster add)
  - Scott Matlock (RB)   -- Los Angeles Chargers, listed as a fullback by
                            Rotowire; ESPN tags him RB, so filed as RB to
                            match the source. Active Week 5, off IR.
Players are created only if resolve_player finds no match; any non-exact,
non-none match is skipped for a human look.

Part 2 -- 1 'invalid_value' row: Cade Otton (TE, TB) ESPN receiving_tds
projection of -0.002995605 (ESPN's model emitted a tiny negative; the app's
own validation correctly refused it). Accepted with corrected_value=0.0,
same precedent as Tom Kennedy in fix_and_resolve_yahoo_week3.py. Guarded:
only corrected if the stored value is a negligible negative (> -0.01).

Run from the repo root:
    python resolve_week6_review_queue.py > out_resolve_wk6.txt 2>&1
"""
from db.database import get_conn
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"

# raw name -> (position, canonical_display_name)
RESOLUTIONS = {
    "Brandon Smith": ("WR", "Brandon Smith"),
    "Carson Steele": ("RB", "Carson Steele"),
    "Scott Matlock": ("RB", "Scott Matlock"),
}

NEGLIGIBLE_NEGATIVE = -0.01  # only clamp values between this and 0


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

    # ---- Part 2: negligible-negative invalid_value rows ----
    invalid = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.stat_name, po.stat_value, po.source_name
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND ri.reason='invalid_value'"""
    ).fetchall()
    print(f"Pending invalid_value rows: {len(invalid)}")
    fixed, skipped = 0, 0
    for row in invalid:
        v = row["stat_value"]
        label = f"proposed_id={row['proposed_id']} {row['extracted_player_name']} {row['stat_name']} ({row['source_name']}) value={v}"
        if v is None or not (NEGLIGIBLE_NEGATIVE < v < 0):
            skipped += 1
            print(f"[SKIP] {label} -- not a negligible negative, needs a human look.")
            continue
        try:
            pipeline.accept_proposed_observation(conn, row["proposed_id"], corrected_value=0.0)
            fixed += 1
            print(f"  corrected {label} -> 0.0")
        except ValueError as e:
            skipped += 1
            print(f"[FAIL] {label}: {e}")
    print(f"Corrected+accepted {fixed}, skipped/failed {skipped}.")

    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    print(f"Remaining pending review items (all sources): {remaining}")
    conn.close()


if __name__ == "__main__":
    main()
