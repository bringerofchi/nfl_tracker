"""
Full review-queue remediation (2026-09-17). Supersedes the earlier
resolve_yahoo_review_queue.py attempt -- ground-truth inspection of the
real tracker.db showed that script's effects (36 players created, 14
aliases, 196 observations accepted) never actually landed in this
database, despite an earlier session summary claiming it had. Rather
than trust that claim, this re-derives and re-applies the full fix
against the actual current pending review queue, plus the 68 new ESPN
Week 1 identity gaps and 6 Athletic/ESPN "Carson Wentz" gaps opened by
later runs. Explicitly authorized by Justin: "yes, I want you to fix
them all... for the ones that are still in question, use multiple
sources to verify" (Yahoo backlog) and "resolve those the way you did
the yahoo backlog" (ESPN's new gaps), 2026-09-16/17.

Three categories, handled differently, never guessed at automatically:

1. Status-tag suffix ("Tyreek Hill NA", "A.J. Brown IR", ...) -- Yahoo's
   own injury/roster-status abbreviation riding along in the extracted
   name string. Stripping it is mechanical, not a guess.
2. Genuinely new players not yet in the roster -- identity confirmed via
   web search (multiple independent sources: team sites, ESPN, NFL.com,
   Wikipedia, PFR, Spotrac) before creating anything.
3. A genuine duplicate player record (Andrew Beck, RB) created twice by
   two separate ESPN weekly pulls on different dates -- same person,
   confirmed by inspecting both records' projection rows (same stat
   pattern, same source, differing only by week). Merged into one
   canonical player_id; the newer duplicate's rows are repointed, not
   deleted outright, so provenance is preserved.

For every name whose canonical/created display name differs from the raw
OCR/extracted string, a player_name_aliases row is added mapping the raw
string to the resolved player -- the durable fix: any future ingestion
producing that exact same string auto-resolves via the existing
alias-matching path, no code changes needed.

Run from the repo root:
    python resolve_review_queue_full.py
"""
import sqlite3

from db.database import get_conn, normalize_name
from db import repository as repo
from ingestion import pipeline

DB_PATH = "db/tracker.db"

# --- Step 1: merge the genuine duplicate Andrew Beck (RB) player record. ---
# player_id 492 created 2026-09-13 14:07 (week 2 ESPN pull), 507 created
# 2026-09-13 14:25 (week 1 ESPN pull) -- same normalized_name, same
# position, both with only ESPN weekly receiving_tds/receiving_yards
# projections. Keep 492 (created first), repoint 507's projections/
# statistics/rankings to it, then delete the now-empty duplicate.
DUPLICATE_MERGE_KEEP = 492
DUPLICATE_MERGE_DROP = 507

# name Yahoo/ESPN/Athletic produced -> (position, canonical_display_name)
# canonical_display_name == raw name when there's no truncation/tag to strip.
RESOLUTIONS = {
    # --- tag-stripped, resolves to an EXISTING player (verified by exact
    #     normalized-name + position match already in the roster) ---
    "A.J. Brown IR":     ("WR", "A.J. Brown"),
    "CJ Daniels NA":      ("WR", "CJ Daniels"),
    "Eli Stowers IR":     ("TE", "Eli Stowers"),
    "Joe Royer NFI-R":    ("TE", "Joe Royer"),
    "Justin Joly NA":     ("TE", "Justin Joly"),
    "Max Klare NA":       ("TE", "Max Klare"),
    "Skyler Bell NA":     ("WR", "Skyler Bell"),
    "Tank Dell IR-R":     ("WR", "Tank Dell"),

    # --- genuinely new players, identity verified via web search
    #     (team site / ESPN / NFL.com / Wikipedia / Pro-Football-Reference) ---
    "Amon-Ra St.":        ("WR", "Amon-Ra St. Brown"),      # truncated OCR
    "Andy Dalton":        ("QB", "Andy Dalton"),
    "Anthony Gould":      ("WR", "Anthony Gould"),
    "Audric Estime":      ("RB", "Audric Estime"),
    "Austin Ekeler":      ("RB", "Austin Ekeler"),
    "Brandin Cooks":      ("WR", "Brandin Cooks"),
    "Brandon Aiyuk":      ("WR", "Brandon Aiyuk"),
    "Camden Brown":       ("WR", "Camden Brown"),
    "Carson Wentz":       ("QB", "Carson Wentz"),
    "Charlie Jones":      ("WR", "Charlie Jones"),
    "Chris Brazzell":     ("WR", "Chris Brazzell"),
    "Dee Eskridge":       ("WR", "Dee Eskridge"),
    "Desmond Reid":       ("RB", "Desmond Reid"),
    "Dillon Gabriel":     ("QB", "Dillon Gabriel"),
    "Dontayvion":         ("WR", "Dontayvion Wicks"),        # truncated OCR (first name only)
    "J'Mari Taylor":      ("RB", "J'Mari Taylor"),
    "Jack Endries":       ("TE", "Jack Endries"),
    "Jayden Higgins":     ("WR", "Jayden Higgins"),
    "Jelani Woods":       ("TE", "Jelani Woods"),
    "Jerome Ford":        ("RB", "Jerome Ford"),
    "Jimmy Garoppolo":    ("QB", "Jimmy Garoppolo"),
    "Joe Milton III":     ("QB", "Joe Milton III"),
    "Kareem Hunt":        ("RB", "Kareem Hunt"),
    "Lewis Bond":         ("WR", "Lewis Bond"),
    "Lil'Jordan Humphrey": ("WR", "Lil'Jordan Humphrey"),
    "Mason Rudolph":      ("QB", "Mason Rudolph"),
    "Matt Hibner":        ("TE", "Matt Hibner"),
    "Mitchell Evans":     ("TE", "Mitchell Evans"),
    "Nick Chubb":         ("RB", "Nick Chubb"),
    "Noah Thomas":        ("WR", "Noah Thomas"),
    "Raheem Mostert":     ("RB", "Raheem Mostert"),
    "Rhamondre":          ("RB", "Rhamondre Stevenson"),     # truncated OCR (first name only)
    "Ricky Pearsall":     ("WR", "Ricky Pearsall"),
    "Ronnie Rivers":      ("RB", "Ronnie Rivers"),
    "Theo Wease":         ("WR", "Theo Wease"),
    "Tyler Lockett":      ("WR", "Tyler Lockett"),
    "Tyreek Hill NA":     ("WR", "Tyreek Hill"),
    "Will Dissly NA":     ("TE", "Will Dissly"),
    "Zach Ertz NA":       ("TE", "Zach Ertz"),

    # --- new from the 2026-09-17 ESPN Week 1 rerun + Athletic gaps ---
    "Brady Russell":      ("RB", "Brady Russell"),     # ESPN's own defaultPositionId classifies him RB
    "CJ Donaldson":       ("RB", "CJ Donaldson"),      # Saints rookie RB, 2026 draft class
    "Chris Moore":        ("WR", "Chris Moore"),        # Ravens WR
    "DeeJay Dallas":      ("RB", "DeeJay Dallas"),
    "Frank Gore Jr.":     ("RB", "Frank Gore Jr."),     # Bills RB
    "Jake Briningstool":  ("TE", "Jake Briningstool"),  # Chiefs TE
    "Jared Wayne":        ("WR", "Jared Wayne"),        # Texans WR
    "Julius Chestnut":    ("RB", "Julius Chestnut"),    # Titans RB
    "Lan Larison":        ("RB", "Lan Larison"),        # Patriots RB
    "Montorie Foster Jr.": ("WR", "Montorie Foster Jr."), # Seahawks WR
    "Quintin Morris":     ("TE", "Quintin Morris"),     # Bills TE
    "Ryan Miller":        ("WR", "Ryan Miller"),        # Giants WR
    "Stetson Bennett IV": ("QB", "Stetson Bennett IV"), # Rams QB
}


def merge_duplicate_player(conn, keep_id, drop_id):
    """Repoints every row referencing drop_id to keep_id, across all three
    observation tables, then deletes the now-empty duplicate player row.
    Never deletes an observation -- only changes whose player_id it's filed
    under, preserving every provenance/version/audit field untouched."""
    kept = conn.execute("SELECT display_name, position FROM players WHERE player_id=?", (keep_id,)).fetchone()
    dropped = conn.execute("SELECT display_name, position FROM players WHERE player_id=?", (drop_id,)).fetchone()
    if not kept or not dropped:
        print(f"[SKIP MERGE] one of player_id {keep_id}/{drop_id} no longer exists -- already merged?")
        return
    if kept["display_name"] != dropped["display_name"] or kept["position"] != dropped["position"]:
        print(f"[SKIP MERGE] {keep_id} ({dict(kept)}) and {drop_id} ({dict(dropped)}) don't match -- not touching")
        return

    for table in ("projections", "statistics", "rankings"):
        conn.execute(f"UPDATE {table} SET player_id=? WHERE player_id=?", (keep_id, drop_id))
    conn.execute("UPDATE player_name_aliases SET player_id=? WHERE player_id=?", (keep_id, drop_id))
    conn.execute("UPDATE proposed_observations SET resolved_player_id=? WHERE resolved_player_id=?", (keep_id, drop_id))
    conn.execute("DELETE FROM players WHERE player_id=?", (drop_id,))
    conn.commit()
    print(f"[MERGED] player_id {drop_id} ({dropped['display_name']}, {dropped['position']}) -> {keep_id}")


def main():
    conn = get_conn(DB_PATH)

    merge_duplicate_player(conn, DUPLICATE_MERGE_KEEP, DUPLICATE_MERGE_DROP)

    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position, ri.review_id, po.source_name,
                  po.identity_match_type, po.resolved_player_id
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending'"""
    ).fetchall()

    # The Andrew Beck rows were 'ambiguous' with resolved_player_id NULL at
    # proposal time -- after the merge above, resolve_player will now return
    # a clean 'exact' for them, so they don't need a RESOLUTIONS entry at all.
    still_unmapped_and_not_beck = sorted({
        r["extracted_player_name"] for r in pending
        if r["extracted_player_name"] != "Andrew Beck"
    } - set(RESOLUTIONS))
    if still_unmapped_and_not_beck:
        print("[FAIL] These pending names have no resolution mapped -- aborting, nothing written:")
        for n in still_unmapped_and_not_beck:
            print("  ", n)
        conn.close()
        return

    resolved_ids = {}   # raw name -> player_id
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

    # Andrew Beck: resolve fresh now that the duplicate is merged away.
    beck_id, beck_match = repo.resolve_player(conn, "Andrew Beck", "RB")
    if beck_match == "exact":
        resolved_ids["Andrew Beck"] = beck_id
    else:
        print(f"[WARN] Andrew Beck resolved as '{beck_match}' after merge -- not accepting those rows")

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
    total_players = conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    print(f"Remaining pending review items: {remaining}")
    print(f"Total players now in roster: {total_players}")
    conn.close()


if __name__ == "__main__":
    main()
