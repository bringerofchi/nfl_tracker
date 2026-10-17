"""
Two things in one script, both stemming from the same root cause (Yahoo's
PDF export re-rendering the last row of some pages with mangled decimal
points across an internal page break -- see overview.md session notes,
2026-09-24):

PART A -- correct two rows that were already silently auto-accepted into
the real projections table with WRONG values from a parser bug (the first,
weaker version of the malformed-row detector missed these two specific
rows because their corrupted tokens happened to still satisfy the check).
Diagnosed by re-deriving the correct values from the clean page-break
duplicate render, confirmed against the raw PDF text directly. This is a
same-day bug fix with concrete, defensible correct values (not a second
legitimate data snapshot / "which is right" judgment call), but per this
project's standing rule of never silently touching stored data without
saying so, both corrections are printed with their before/after values
before being applied.

  - Germie Bernard (Pit WR): rush_attempts, rush_yards, rush_tds,
    receptions, receiving_yards, receiving_tds, fumbles_lost were all
    wrong (e.g. receptions was stored as 19.0 -- an absurd single-week
    value that should have been caught by validation but wasn't).
    Correct values: rush_attempts=0.1, rush_yards=0.3, rush_tds=0.0,
    receptions=1.6, receiving_yards=19.9, receiving_tds=0.1,
    fumbles_lost=0.0.

Britain Covey is NOT touched here -- flagged separately to Justin as a
lower-confidence case (only an unemitted field was visibly corrupted, but
full row confidence isn't high enough to silently fix or silently leave).

PART B -- resolve the 7-item Yahoo Week 3 review queue:
  - 6 genuinely new/veteran players not yet in the roster, identity
    confirmed via web search (team site / ESPN / CBS Sports / official
    beat coverage) before creating anything, per the never-guess
    invariant.
  - 1 player (Tom Kennedy) already matched an existing player (exact
    identity match) but was flagged because its stat value
    (receiving_tds=8.0) was implausible -- same root-cause bug as Part A.
    Corrected value (0.0) is passed explicitly via accept_proposed_
    observation's corrected_value parameter rather than accepting the
    stored bad value.

Run from the repo root:
    python fix_and_resolve_yahoo_week3.py
"""
from db.database import get_conn, normalize_name
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"
SEASON_YEAR = 2026
WEEK_NUMBER = 3

# --- Part A: direct correction of already-accepted bad values ---------

GERMIE_BERNARD_FIX = {
    "rush_attempts": 0.1,
    "rush_yards": 0.3,
    "rush_tds": 0.0,
    "receptions": 1.6,
    "receiving_yards": 19.9,
    "receiving_tds": 0.1,
    "fumbles_lost": 0.0,
}

# --- Part B: review-queue resolutions ----------------------------------
# raw name Yahoo produced -> (position, canonical_display_name, team)
RESOLUTIONS = {
    "Case Keenum":         ("QB", "Case Keenum", "Chi"),          # Bears QB, in line to start Week 3 (Bagent in concussion protocol)
    "Cornell Powell":      ("WR", "Cornell Powell", "Pit"),       # Steelers WR (signed 2026)
    "E.J. Jenkins":        ("TE", "E.J. Jenkins", "Phi"),         # Eagles TE
    "Elijah Mitchell":     ("RB", "Elijah Mitchell", "Phi"),      # Eagles RB (signed after Patriots release)
    "M. Valdes-Scantling": ("WR", "Marquez Valdes-Scantling", "LAC"),  # truncated OCR; Chargers WR (signed after Cowboys release)
    "Russell Wilson":      ("QB", "Russell Wilson", "NYG"),       # Giants QB
}

TOM_KENNEDY_CORRECTED_RECEIVING_TDS = 0.0


def fix_germie_bernard(conn):
    week_row = conn.execute(
        "SELECT week_id FROM weeks w JOIN seasons s ON s.season_id=w.season_id "
        "WHERE s.year=? AND w.week_number=?", (SEASON_YEAR, WEEK_NUMBER)
    ).fetchone()
    if not week_row:
        print("[FAIL] Could not find Week 3 week_id.")
        return
    week_id = week_row["week_id"]

    player_row = conn.execute(
        "SELECT player_id FROM players WHERE display_name='Germie Bernard'"
    ).fetchone()
    if not player_row:
        print("[SKIP] Germie Bernard not found in players table -- nothing to fix.")
        return
    player_id = player_row["player_id"]

    print("=== Germie Bernard (Pit WR) -- before/after correction ===")
    for stat_name, correct_value in GERMIE_BERNARD_FIX.items():
        row = conn.execute(
            """SELECT projection_id, projected_value FROM projections
               WHERE player_id=? AND source_name='Yahoo' AND scope='weekly'
                 AND week_id=? AND stat_name=?""",
            (player_id, week_id, stat_name),
        ).fetchone()
        if row is None:
            print(f"  {stat_name}: no existing row (expected value {correct_value}) -- skipping, not inserting blind")
            continue
        before = row["projected_value"]
        if before == correct_value:
            print(f"  {stat_name}: already correct ({before})")
            continue
        conn.execute(
            "UPDATE projections SET projected_value=?, notes=COALESCE(notes,'') || ' [corrected by claude 2026-09-24: parser page-break glitch fix]' WHERE projection_id=?",
            (correct_value, row["projection_id"]),
        )
        print(f"  {stat_name}: {before} -> {correct_value}")
    conn.commit()


def resolve_review_queue(conn):
    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position, ri.reason
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND po.source_name='Yahoo'"""
    ).fetchall()

    names_present = {r["extracted_player_name"] for r in pending}
    unmapped = names_present - set(RESOLUTIONS) - {"Tom Kennedy"}
    if unmapped:
        print("[FAIL] These pending names have no resolution mapped -- aborting Part B, nothing written:")
        for n in unmapped:
            print("  ", n)
        return

    resolved_ids = {}
    created, reused, aliased = 0, 0, 0
    for raw_name, (position, canonical, team) in RESOLUTIONS.items():
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
        name = row["extracted_player_name"]
        if name == "Tom Kennedy":
            try:
                pipeline.accept_proposed_observation(
                    conn, row["proposed_id"], corrected_value=TOM_KENNEDY_CORRECTED_RECEIVING_TDS
                )
                accepted += 1
            except ValueError as e:
                failed += 1
                print(f"[FAIL] proposed_id={row['proposed_id']} (Tom Kennedy): {e}")
            continue

        player_id = resolved_ids.get(name)
        if player_id is None:
            continue
        try:
            pipeline.accept_proposed_observation(conn, row["proposed_id"], player_id_override=player_id)
            accepted += 1
        except ValueError as e:
            failed += 1
            print(f"[FAIL] proposed_id={row['proposed_id']} ({name}): {e}")

    print(f"Accepted {accepted} observations, {failed} failed.")
    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    print(f"Remaining pending review items (all sources): {remaining}")


def main():
    conn = get_conn(DB_PATH)
    fix_germie_bernard(conn)
    print()
    resolve_review_queue(conn)
    conn.close()
    print()
    print("Done. Paste this output back to Claude.")


if __name__ == "__main__":
    main()
