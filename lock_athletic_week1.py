"""
Officially locks The Athletic's Week 1 weekly projections using the standard
per-source policy (repository.ensure_weekly_projection_lock). Week 1 Athletic
data was ingested 2026-09-16, before per-source locks existed, so there is no
weekly_projection_locks row for ('The Athletic', week 1) and its 1207 current
rows are unfrozen. ESPN Week 1 already has its lock row (2026-09-17).

Run from the repo root:
    python lock_athletic_week1.py > out_lock_athletic_wk1.txt 2>&1
"""
from db.database import get_conn
from db import repository as repo

conn = get_conn("db/tracker.db")
wk = conn.execute("SELECT week_id FROM weeks WHERE season_id=1 AND week_number=1").fetchone()["week_id"]

def frozen():
    return conn.execute(
        "SELECT is_frozen, COUNT(*) c FROM projections WHERE source_name='The Athletic' AND week_id=? AND scope='weekly' AND is_current=1 GROUP BY 1",
        (wk,),
    ).fetchall()

print("Before:", [(r["is_frozen"], r["c"]) for r in frozen()])
print("Locked now:", repo.ensure_weekly_projection_lock(conn, "The Athletic", wk))
print("After: ", [(r["is_frozen"], r["c"]) for r in frozen()])
print("Lock rows:", [tuple(r) for r in conn.execute("SELECT source_name, week_id, locked_at FROM weekly_projection_locks WHERE week_id=?", (wk,))])
conn.close()
