"""
READ-ONLY post-migration validation. Compares the live DB against the backup
that `migrate_freeze_late_weekly_projections.py --apply` wrote immediately
before changing anything, so the pre-state is authoritative, not remembered.

Run from the repo root (exit code is non-zero if any check FAILs):
    python validate_lockgap_migration.py > out_lockgap_validate.txt 2>&1
Options: --db PATH  --backup PATH  --expected N (default 67)
"""
import argparse
import glob
import hashlib
import os
import sqlite3
import sys

from db import repository as repo
from fantasy import service as fsvc
from fantasy.scoring import calculate_fantasy_points

FAILS = []


def check(label, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def fingerprint(c):
    h = hashlib.sha256()
    for r in c.execute("SELECT projection_id, projected_value FROM projections ORDER BY projection_id"):
        h.update(f"{r[0]}:{r[1]!r};".encode())
    return h.hexdigest()


def one(c, sql, *a):
    return c.execute(sql, a).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="db/tracker.db")
    ap.add_argument("--backup", default=None)
    ap.add_argument("--expected", type=int, default=67)
    args = ap.parse_args()

    backup = args.backup
    if not backup:
        found = sorted(glob.glob(f"{args.db}.bak-*-pre-lockgap"), key=os.path.getmtime)
        if not found:
            raise SystemExit("[ABORT] no *-pre-lockgap backup found next to the DB; pass --backup")
        backup = found[-1]
    print(f"live:   {args.db}\nbackup: {backup}\n")
    live, old = ro(args.db), ro(backup)

    n = args.expected
    check("projection row count unchanged",
          one(live, "SELECT COUNT(*) FROM projections") == one(old, "SELECT COUNT(*) FROM projections"),
          f"{one(old, 'SELECT COUNT(*) FROM projections')} -> {one(live, 'SELECT COUNT(*) FROM projections')}")
    s_old, s_new = one(old, "SELECT TOTAL(projected_value) FROM projections"), one(live, "SELECT TOTAL(projected_value) FROM projections")
    check("projected_value sum unchanged", s_old == s_new, f"{s_old!r} -> {s_new!r}")
    check("projected_value fingerprint (every id/value pair) unchanged", fingerprint(old) == fingerprint(live))
    c_old, c_new = one(old, "SELECT COUNT(*) FROM corrections"), one(live, "SELECT COUNT(*) FROM corrections")
    check(f"corrections increased by exactly {n}", c_new - c_old == n, f"{c_old} -> {c_new}")
    check(f"{n} rows newly frozen",
          one(live, "SELECT COUNT(*) FROM projections WHERE is_frozen=1") - one(old, "SELECT COUNT(*) FROM projections WHERE is_frozen=1") == n)
    tag = "%" + repo.LATE_AFTER_LOCK_TAG + "%"
    check(f"{n} rows newly tagged late-after-lock",
          one(live, "SELECT COUNT(*) FROM projections WHERE notes LIKE ?", tag)
          - one(old, "SELECT COUNT(*) FROM projections WHERE notes LIKE ?", tag) == n)
    gap = one(live, """SELECT COUNT(*) FROM projections p WHERE p.scope='weekly' AND p.is_current=1 AND p.is_frozen=0
                       AND EXISTS (SELECT 1 FROM weekly_projection_locks l WHERE l.week_id=p.week_id
                                   AND l.source_name IN (p.source_name, REPLACE(p.source_name,' (live)','')))""")
    check("no unfrozen current rows remain in any locked source/week", gap == 0, f"remaining={gap}")
    w4 = live.execute("""SELECT w.status FROM weeks w JOIN seasons s ON s.season_id=w.season_id
                         WHERE s.year=2026 AND w.week_number=4 AND w.week_type='regular'""").fetchone()["status"]
    check("Week 4 status is 'upcoming'", w4 == "upcoming", w4)
    check("no week left as 'completed'", one(live, "SELECT COUNT(*) FROM weeks WHERE status='completed'") == 0)

    def player(name):
        r = live.execute("SELECT player_id, position FROM players WHERE display_name=?", (name,)).fetchone()
        return r["player_id"], r["position"]

    def vals(name, source="ESPN", week=4, comparison_only=True):
        pid, pos = player(name)
        wid = one(live, "SELECT week_id FROM weeks w JOIN seasons s USING(season_id) WHERE s.year=2026 AND w.week_number=?", week)
        return fsvc.get_projection_stat_values(live, pid, wid, pos, source, comparison_only=comparison_only)

    v = vals("Velus Jones Jr.")
    check("Velus Jones Jr.: no eligible Week 4 projection", all(x is None for x in v.values()), str(v))
    raw = vals("Velus Jones Jr.", comparison_only=False)
    check("Velus Jones Jr.: his 5 rows still exist (only excluded from comparison)",
          sum(x is not None for x in raw.values()) == 5)

    b = vals("Deion Burks")
    kept = {k for k in ("rush_yards", "rush_tds", "rush_attempts") if b.get(k) is not None}
    check("Deion Burks: keeps his three eligible components",
          kept == {"rush_yards", "rush_tds", "rush_attempts"}, str(b))
    check("Deion Burks: late components excluded (missing, not zero)",
          b["receiving_yards"] is None and b["receiving_tds"] is None)
    res = calculate_fantasy_points(b)
    check("Deion Burks: projection flagged incomplete, late components listed as missing",
          (not res.is_complete) and {"receiving_yards", "receiving_tds"} <= set(res.missing),
          f"missing={res.missing}")
    print("  note: ESPN never supplies receptions/fumbles_lost, so every ESPN projection is already "
          "'incomplete'; the `missing` list above is what distinguishes Burks.")

    live.close(); old.close()
    print()
    if FAILS:
        print(f"RESULT: {len(FAILS)} FAILED -> {FAILS}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
