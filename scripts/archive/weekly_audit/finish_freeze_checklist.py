"""
One-shot: (1) permanently adds rush_attempts to the real stat_definitions
catalog in db/tracker.db (additive only -- INSERT OR IGNORE, touches no
existing player/stat/observation data), then (2) runs the real ESPN
weekly live network check against ESPN's actual endpoint.

Run from the repo root:
    python finish_freeze_checklist.py

Needs real internet access for part 2 (the ESPN call) -- this is the
one piece that could not be run from the sandbox.
"""
import sqlite3
import sys

from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter
from ingestion.collector import AdapterUnavailable


def part1_fix_registry():
    print("=" * 60)
    print("PART 1: permanent rush_attempts registry fix (real db/tracker.db)")
    print("=" * 60)
    conn = sqlite3.connect("db/tracker.db")
    added = 0
    for position in ("QB", "RB", "WR", "TE"):
        cur = conn.execute(
            "INSERT OR IGNORE INTO stat_definitions (position, stat_name, display_label) "
            "VALUES (?, 'rush_attempts', 'Rush Attempts')",
            (position,),
        )
        added += cur.rowcount
    conn.commit()

    rows = conn.execute("SELECT position FROM stat_definitions WHERE stat_name='rush_attempts'").fetchall()
    conn.close()
    print(f"Added {added} new row(s) this run.")
    print(f"rush_attempts is now registered for: {[r[0] for r in rows]}")
    print("(Existing player/stat/observation data untouched -- catalog-only change.)")
    print()


def part2_espn_live_check():
    print("=" * 60)
    print("PART 2: real ESPN weekly live network check")
    print("=" * 60)
    print("Fetching Jahmyr Gibbs, week 5, 2025 from ESPN's real live endpoint...")

    adapter = ESPNWeeklyLiveAdapter(source_name="ESPN", player_ids=[4429795])
    try:
        observations = adapter.fetch(season_id=2025, week_number=5)
    except AdapterUnavailable as e:
        print(f"[FAIL] AdapterUnavailable: {e} (transient={e.transient})")
        print("If this says 'Host not in allowlist' or similar, this environment still can't")
        print("reach ESPN -- this needs to run from a machine with normal, unrestricted internet.")
        sys.exit(1)

    print(f"[OK] fetch() returned {len(observations)} observations")
    by_type = {}
    for o in observations:
        by_type.setdefault(o.data_type, 0)
        by_type[o.data_type] += 1
    print(f"By data_type: {by_type}")
    print()
    for o in observations:
        print(f"  {o.player_name_raw:20s} {o.data_type:10s} week={o.week_number:2d} "
              f"{o.stat_name:20s} = {o.value}")

    leak = [o for o in observations if o.stat_name == "appliedTotal"]
    print()
    if leak:
        print(f"[FAIL] {len(leak)} observation(s) used 'appliedTotal' as stat_name!")
        sys.exit(1)
    print("[OK] no observation used 'appliedTotal' as a stat_name")

    # Sanity check against known real values from FINDINGS.md / this session's
    # own mapping-evidence pass for this exact player/week.
    actual = {o.stat_name: o.value for o in observations if o.data_type == "actual"}
    expected = {"rush_attempts": 12, "rush_yards": 54, "receptions": 2,
                "receiving_yards": 33, "receiving_tds": 1}
    print()
    print("Cross-check against known real values for this game:")
    all_match = True
    for stat, exp_val in expected.items():
        got = actual.get(stat)
        ok = got == exp_val
        all_match = all_match and ok
        print(f"  {stat:20s} expected={exp_val:6} got={got}  {'OK' if ok else 'MISMATCH'}")
    print()
    print("[ALL MATCH]" if all_match else "[MISMATCH -- investigate]")


if __name__ == "__main__":
    part1_fix_registry()
    part2_espn_live_check()
    print()
    print("Done. Paste all of this output back to Claude.")
