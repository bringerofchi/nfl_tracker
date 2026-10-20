"""
Automated cleanup: applies the two small source edits (db/database.py's
STAT_DEFINITIONS/STAT_LABELS, ingestion/scheduler.py's import + registry
entry) safely -- only if it finds the exact text it expects. If a file
has already diverged from what's expected, it says so clearly and skips
that file rather than guessing or corrupting anything. Then runs the
full test suite.

Run from the repo root:
    python auto_cleanup.py
"""
import re
import subprocess
import sys


def patch_file(path, replacements, label):
    print(f"--- {label} ({path}) ---")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[SKIP] {path} not found.")
        return

    changed = False
    for old, new, already_done_marker in replacements:
        if already_done_marker in content:
            print(f"[SKIP] already applied (found {already_done_marker!r}).")
            continue
        if old not in content:
            print(f"[SKIP] expected text not found -- this file may have diverged. "
                  f"No changes made to be safe. You'll need to apply this edit by hand.")
            continue
        content = content.replace(old, new, 1)
        changed = True
        print("[OK] applied.")

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[SAVED] {path}")
    print()


def main():
    # --- db/database.py ---
    old_stat_defs = '''STAT_DEFINITIONS = {
    "QB": ["pass_yards", "pass_tds", "interceptions", "rush_yards", "rush_tds", "pass_completions", "pass_attempts"],
    "RB": ["rush_yards", "rush_tds", "receptions", "receiving_yards", "receiving_tds", "fumbles_lost"],
    "WR": ["receptions", "receiving_yards", "receiving_tds", "rush_yards", "rush_tds", "fumbles_lost"],
    "TE": ["receptions", "receiving_yards", "receiving_tds", "fumbles_lost"],
}'''
    new_stat_defs = '''STAT_DEFINITIONS = {
    "QB": ["pass_yards", "pass_tds", "interceptions", "rush_yards", "rush_tds", "rush_attempts",
           "pass_completions", "pass_attempts"],
    "RB": ["rush_yards", "rush_tds", "rush_attempts", "receptions", "receiving_yards", "receiving_tds",
           "fumbles_lost"],
    "WR": ["receptions", "receiving_yards", "receiving_tds", "rush_yards", "rush_tds", "rush_attempts",
           "fumbles_lost"],
    "TE": ["receptions", "receiving_yards", "receiving_tds", "rush_attempts", "fumbles_lost"],
}'''

    old_labels = '''STAT_LABELS = {
    "pass_yards": "Pass Yards", "pass_tds": "Pass TDs", "interceptions": "INTs",
    "rush_yards": "Rush Yards", "rush_tds": "Rush TDs",
    "pass_completions": "Completions", "pass_attempts": "Attempts",
    "receptions": "Receptions", "receiving_yards": "Receiving Yards",
    "receiving_tds": "Receiving TDs", "fumbles_lost": "Fumbles Lost",
}'''
    new_labels = '''STAT_LABELS = {
    "pass_yards": "Pass Yards", "pass_tds": "Pass TDs", "interceptions": "INTs",
    "rush_yards": "Rush Yards", "rush_tds": "Rush TDs", "rush_attempts": "Rush Attempts",
    "pass_completions": "Completions", "pass_attempts": "Attempts",
    "receptions": "Receptions", "receiving_yards": "Receiving Yards",
    "receiving_tds": "Receiving TDs", "fumbles_lost": "Fumbles Lost",
}'''

    patch_file("db/database.py", [
        (old_stat_defs, new_stat_defs, '"TE": ["receptions", "receiving_yards", "receiving_tds", "rush_attempts"'),
        (old_labels, new_labels, '"rush_attempts": "Rush Attempts"'),
    ], "db/database.py: STAT_DEFINITIONS + STAT_LABELS")

    # --- ingestion/scheduler.py ---
    old_import = "from ingestion.espn_projections_adapter import ESPNSeasonProjectionsAdapter"
    new_import = ("from ingestion.espn_projections_adapter import ESPNSeasonProjectionsAdapter\n"
                   "from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter")

    old_registry = '''ADAPTER_REGISTRY = {
    "LocalFileSourceAdapter": LocalFileSourceAdapter,
    "AthleticSeasonXlsxAdapter": AthleticSeasonXlsxAdapter,
    "ESPNSeasonProjectionsAdapter": ESPNSeasonProjectionsAdapter,
}'''
    new_registry = '''ADAPTER_REGISTRY = {
    "LocalFileSourceAdapter": LocalFileSourceAdapter,
    "AthleticSeasonXlsxAdapter": AthleticSeasonXlsxAdapter,
    "ESPNSeasonProjectionsAdapter": ESPNSeasonProjectionsAdapter,
    "ESPNWeeklyLiveAdapter": ESPNWeeklyLiveAdapter,
}'''

    patch_file("ingestion/scheduler.py", [
        (old_import, new_import, "from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter"),
        (old_registry, new_registry, '"ESPNWeeklyLiveAdapter": ESPNWeeklyLiveAdapter'),
    ], "ingestion/scheduler.py: import + ADAPTER_REGISTRY entry")

    print("=" * 60)
    print("Running full test suite...")
    print("=" * 60)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], capture_output=True, text=True)
    print(result.stdout[-3000:])
    print(result.stderr[-1000:])


if __name__ == "__main__":
    main()
