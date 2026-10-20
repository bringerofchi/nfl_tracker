"""
Read-only diagnostic: which raw ESPN stat IDs appear in PROJECTED vs ACTUAL weekly
entries, and whether the mapped stats reproduce ESPN's own appliedTotal.

Why: the tracker's ESPN weekly projections never contain `receptions` (0 rows for
RB/WR/TE) although ESPN actuals do. Either ESPN publishes projected receptions under
an ID the stat map doesn't cover, or it doesn't publish it at all. This shows which,
using real data. It fetches from ESPN and prints; it never touches tracker.db and
never writes anything. Must run on your machine (ESPN is blocked from the sandbox).

  python scripts/diag_espn_projected_stat_ids.py --week 6 > out_diag_espn_ids.txt 2>&1
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fantasy.scoring import calculate_fantasy_points  # noqa: E402
from ingestion.espn_weekly_core import find_weekly_stat_entry  # noqa: E402
from ingestion.espn_weekly_live_adapter import ESPNWeeklyLiveAdapter  # noqa: E402
from ingestion.espn_weekly_stat_map import STAT_ID_TO_NAME  # noqa: E402

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE"}


def mapped_points(inner):
    vals = {STAT_ID_TO_NAME[k]: float(v) for k, v in inner.items() if k in STAT_ID_TO_NAME}
    return round(calculate_fantasy_points(vals).total_points, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--samples", type=int, default=4)
    a = ap.parse_args()

    entries = ESPNWeeklyLiveAdapter(season_year=a.year)._fetch_raw(a.year)
    print(f"entries fetched: {len(entries)}  week={a.week} year={a.year}")

    for label, src in (("PROJECTED (statSourceId=1)", 1), ("ACTUAL (statSourceId=0)", 0)):
        print(f"\n=== {label} ===")
        for pos_id in (2, 3, 4):
            ids = collections.Counter()
            n = 0
            for e in entries:
                p = e.get("player", {})
                if p.get("defaultPositionId") != pos_id:
                    continue
                se = find_weekly_stat_entry(e, a.year, a.week, src)
                if se is None:
                    continue
                n += 1
                ids.update(se.get("stats", {}).keys())
            print(f"{POS[pos_id]}: {n} entries with this source; stat IDs seen "
                  f"(id:count, * = not in STAT_ID_TO_NAME): "
                  + ", ".join(f"{k}{'' if k in STAT_ID_TO_NAME else '*'}:{c}" for k, c in sorted(ids.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999)))

    print("\n=== ACTUAL: is ID 53 the same number as ID 41 (verified receptions)? ===")
    both = same = 0
    diffs = []
    for e in entries:
        p = e.get("player", {})
        if p.get("defaultPositionId") not in (2, 3, 4):
            continue
        se = find_weekly_stat_entry(e, a.year, a.week, 0)
        inner = se.get("stats", {}) if se else {}
        if "41" in inner and "53" in inner:
            both += 1
            if float(inner["41"]) == float(inner["53"]):
                same += 1
            elif len(diffs) < 10:
                diffs.append((p.get("fullName"), inner["41"], inner["53"]))
    only41 = sum(1 for e in entries if (lambda s: s and "41" in s.get("stats", {}) and "53" not in s.get("stats", {}))(
        find_weekly_stat_entry(e, a.year, a.week, 0)))
    only53 = sum(1 for e in entries if (lambda s: s and "53" in s.get("stats", {}) and "41" not in s.get("stats", {}))(
        find_weekly_stat_entry(e, a.year, a.week, 0)))
    print(f"entries with both 41 and 53: {both}; identical values: {same}; "
          f"only 41: {only41}; only 53: {only53}; differing examples (name, 41, 53): {diffs}")

    print("\n=== PROJECTED samples: mapped-stat PPR vs ESPN appliedTotal (WR/RB/TE) ===")
    shown = 0
    ranked = []
    for e in entries:
        p = e.get("player", {})
        if p.get("defaultPositionId") not in (2, 3, 4):
            continue
        se = find_weekly_stat_entry(e, a.year, a.week, 1)
        if se and isinstance(se.get("stats"), dict):
            ranked.append((se.get("appliedTotal") or 0, p.get("fullName"), POS[p["defaultPositionId"]], se))
    for total, name, pos, se in sorted(ranked, key=lambda x: -x[0])[: a.samples * 3]:
        if shown >= a.samples * 3:
            break
        inner = se["stats"]
        mp = mapped_points(inner)
        print(f"{name} ({pos}) appliedTotal={se.get('appliedTotal')} mapped_ppr={mp} "
              f"gap={None if se.get('appliedTotal') is None else round(se['appliedTotal'] - mp, 2)}")
        print(f"   raw stats: {inner}")
        shown += 1


if __name__ == "__main__":
    sys.exit(main())
