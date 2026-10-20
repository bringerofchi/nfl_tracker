"""
Read-only, reproducible per-source projection accuracy report.

Opens the database read-only and changes nothing. Requires the canonical actuals
source to hold the actuals (run scripts/set_canonical_actuals_source.py first);
the script refuses to report if it finds zero comparable player-weeks.

  python scripts/compare_source_accuracy.py
  python scripts/compare_source_accuracy.py --out exports/source_accuracy_report.md

Method (all through the app's own fantasy.service, no duplicated scoring logic):
  - player-week = one player, one week, positions QB/RB/WR/TE
  - a source's player-week is comparable if it has a projection AND an actual
  - error = actual - projected PPR points; MAE = mean absolute error
  - missing projected stats count as 0 (app behaviour). projection_complete=False
    means at least one scorable stat was missing for that source
  - sources are never merged. Headline numbers use only player-weeks where ALL
    sources are comparable, so each source is scored on the same players
  - the complete-only view further requires every source's projection to be complete
  - uses the app's late-after-lock exclusion (comparison_only=True)
  - the "shared stats" views drop receptions from BOTH the projected and the actual
    score (via the app's own calculate_fantasy_points), so ESPN, whose Weeks 2-6
    projections carry no receptions, is scored on the same basis as Yahoo and
    The Athletic. This is the basis of the 2026-10-07 baseline.
"""
import argparse
import os
import sqlite3
import statistics as st
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
DB_PATH = os.environ.get("NFL_TRACKER_DB_PATH") or os.path.join(REPO_ROOT, "db", "tracker.db")

from fantasy import service  # noqa: E402
from fantasy.scoring import STAT_TO_SCORING_KEY, calculate_fantasy_points  # noqa: E402

POSITIONS = ("QB", "RB", "WR", "TE")
DROPPED_FOR_SHARED = ("receptions",)


def _without(values):
    return {k: v for k, v in values.items() if k not in DROPPED_FOR_SHARED}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="also write the report to this markdown file")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    canon = service._canonical_actuals_source_id(conn)
    canon_name = conn.execute("SELECT name FROM sources WHERE source_id=?", (canon,)).fetchone()["name"]
    weeks = [r["week_number"] for r in conn.execute(
        """SELECT DISTINCT w.week_number FROM statistics s JOIN weeks w ON w.week_id=s.week_id
           WHERE s.source_id=? AND s.is_current=1 ORDER BY 1""", (canon,))]
    week_id = {r["week_number"]: r["week_id"] for r in conn.execute("SELECT week_id, week_number FROM weeks")}
    sources = [r["source_name"] for r in conn.execute(
        """SELECT DISTINCT source_name FROM projections WHERE scope='weekly' AND is_current=1
           AND source_name IN ('ESPN','The Athletic','Yahoo') ORDER BY 1""")]
    players = conn.execute(
        f"SELECT player_id, position FROM players WHERE position IN ({','.join('?'*len(POSITIONS))})", POSITIONS
    ).fetchall()

    rows = {}  # (player_id, week) -> {source: comparison dict}
    for wk in weeks:
        for p in players:
            for src in sources:
                c = service.compare_projection_actual(conn, p["player_id"], week_id[wk], p["position"], src)
                if c["absolute_difference"] is not None:
                    vals = service.get_projection_stat_values(conn, p["player_id"], week_id[wk],
                                                              p["position"], src)
                    c["missing"] = [s for s, v in vals.items() if s in STAT_TO_SCORING_KEY and v is None]
                    c["position"] = p["position"]
                    act = service.get_actual_stat_values(conn, p["player_id"], week_id[wk], p["position"])
                    nr_p = calculate_fantasy_points(_without(vals)).total_points
                    nr_a = calculate_fantasy_points(_without(act)).total_points
                    c["nr_difference"] = round(nr_a - nr_p, 2)
                    c["nr_absolute_difference"] = round(abs(nr_a - nr_p), 2)
                    rows.setdefault((p["player_id"], wk), {})[src] = c

    if not rows:
        print(f"ERROR: 0 comparable player-weeks. Canonical actuals source is {canon_name!r}; "
              "is it the source that holds the actuals?")
        return 1

    out = []
    w = out.append
    w("# Source accuracy report (read-only)")
    w(f"- database: {DB_PATH}")
    w(f"- canonical actuals source: {canon_name}; weeks with actuals: {weeks}")
    w(f"- sources compared: {', '.join(sources)} (Yahoo and The Athletic both derive from Rotowire; not independent)")
    w("")
    w("## Coverage (comparable player-weeks per source, any overlap)")
    for src in sources:
        n = sum(1 for v in rows.values() if src in v)
        n_c = sum(1 for v in rows.values() if src in v and v[src]["projection_complete"])
        w(f"- {src}: {n} comparable, {n_c} with complete projections")

    w("")
    w("## Missing scorable stats per source (counted over that source's comparable player-weeks)")
    w("A missing stat is scored as 0 in the error numbers below. A stat missing for every row of a")
    w("position is a systematic adapter/source gap, not noise. ESPN weekly projections for Weeks 2-6")
    w("have no receptions (mapped from Week 7 on, see ingestion/espn_weekly_stat_map.py); those rows are")
    w("locked and stay receptions-less.")
    for src in sources:
        by_pos = {}
        for v in rows.values():
            if src not in v:
                continue
            ent = by_pos.setdefault(v[src]["position"], [0, {}])
            ent[0] += 1
            for s in v[src]["missing"]:
                ent[1][s] = ent[1].get(s, 0) + 1
        for pos in POSITIONS:
            if pos in by_pos:
                n_pos, miss = by_pos[pos]
                desc = ", ".join(f"{s} {c}/{n_pos}" for s, c in sorted(miss.items())) or "none"
                w(f"- {src} {pos} (n={n_pos}): {desc}")

    def block(title, filt, pos=None, srcs=None, shared=False):
        srcs = srcs or sources
        ak, dk = ("nr_absolute_difference", "nr_difference") if shared else ("absolute_difference", "difference")
        keys = [k for k, v in rows.items() if all(s in v for s in srcs) and filt(v)
                and (pos is None or v[srcs[0]]["position"] == pos)]
        w("")
        w(f"## {title}")
        w(f"Same player-weeks for every source shown: n = {len(keys)}")
        if not keys:
            return
        w("")
        w("| Source | MAE | Bias (actual - projected) | Complete projections |")
        w("|---|---|---|---|")
        for src in srcs:
            a = [rows[k][src][ak] for k in keys]
            d = [rows[k][src][dk] for k in keys]
            comp = sum(1 for k in keys if rows[k][src]["projection_complete"])
            w(f"| {src} | {st.mean(a):.2f} | {st.mean(d):+.2f} | {comp}/{len(keys)} ({100*comp/len(keys):.0f}%) |")

    block("All sources comparable (headline)", lambda v: True)

    w("")
    w("## Shared stats: receptions removed from projected AND actual points")
    w("Fair three-way view for Weeks 2-6. Same basis as the 2026-10-07 baseline (Weeks 2-4: ESPN 4.62,")
    w("Yahoo 4.49, Athletic 4.52). Bias is actual - projected on the receptions-free score, so it is")
    w("not comparable to the PPR bias above. The complete columns still count receptions as a stat.")
    block("Shared stats, all sources comparable", lambda v: True, shared=True)
    for pos in POSITIONS:
        block(f"Shared stats, position {pos}", lambda v: True, pos=pos, shared=True)
    w("")
    w("### Shared stats: MAE by week")
    w("| Week | n | " + " | ".join(sources) + " |")
    w("|---|---|" + "---|" * len(sources))
    for wk in weeks:
        keys = [k for k, v in rows.items() if k[1] == wk and all(s in v for s in sources)]
        if keys:
            cells = [f"{st.mean(rows[k][s]['nr_absolute_difference'] for k in keys):.2f}" for s in sources]
            w(f"| {wk} | {len(keys)} | " + " | ".join(cells) + " |")
        else:
            w(f"| {wk} | 0 | " + " | ".join("-" for _ in sources) + " |")
    w("")
    w("Weeks 2-4 only (to line up with the Oct 7 baseline):")
    keys24 = [k for k, v in rows.items() if 2 <= k[1] <= 4 and all(s in v for s in sources)]
    if keys24:
        w(f"n = {len(keys24)}; " + "; ".join(
            f"{s} {st.mean(rows[k][s]['nr_absolute_difference'] for k in keys24):.2f}" for s in sources))
    block("Complete projections only, all sources",
          lambda v: all(v[s]["projection_complete"] for s in sources))
    w("")
    w("NOTE: fumbles_lost is missing for nearly every RB/WR/TE row in every source, so 'complete'")
    w("above is effectively QB-only. Read the position and fumbles_lost-excluded views below for")
    w("RB/WR/TE.")
    block("Complete except fumbles_lost, all sources",
          lambda v: all(set(v[s]["missing"]) <= {"fumbles_lost"} for s in sources))
    for pos in POSITIONS:
        block(f"Position {pos}: all sources comparable", lambda v: True, pos=pos)
        block(f"Position {pos}: complete except fumbles_lost",
              lambda v: all(set(v[s]["missing"]) <= {"fumbles_lost"} for s in sources), pos=pos)

    w("")
    w("## Athletic vs Yahoo only (ESPN excluded: its Weeks 2-6 projections have no receptions)")
    w("These two are not independent (both derive from Rotowire); the comparison is for sanity,")
    w("not for choosing between independent opinions.")
    duo = [s for s in sources if s != "ESPN"]
    if len(duo) == 2:
        for pos in POSITIONS:
            block(f"Position {pos}: {duo[0]} vs {duo[1]}", lambda v: True, pos=pos, srcs=duo)

    w("")
    w("## MAE by week (all sources comparable)")
    w("| Week | n | " + " | ".join(sources) + " |")
    w("|---|---|" + "---|" * len(sources))
    for wk in weeks:
        keys = [k for k, v in rows.items() if k[1] == wk and all(s in v for s in sources)]
        if keys:
            cells = [f"{st.mean(rows[k][s]['absolute_difference'] for k in keys):.2f}" for s in sources]
            w(f"| {wk} | {len(keys)} | " + " | ".join(cells) + " |")
        else:
            w(f"| {wk} | 0 | " + " | ".join("-" for _ in sources) + " |")

    w("")
    w("Do not declare a winner from these numbers alone: compare the headline and complete-only")
    w("tables, and check that the gap exceeds week-to-week variation and sample size.")

    text = "\n".join(out)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\n(written to {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
