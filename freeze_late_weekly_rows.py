"""
Freezes current scope='weekly' projection rows that were added after their
source+week lock was set (late-added players, corrected rows) and so were
never frozen. Only touches rows whose source+week already has a row in
weekly_projection_locks ("ESPN (live)" / "Yahoo (live)" map to their base source).
Nothing is overwritten -- only is_frozen 0 -> 1.

Run from the repo root:
    python freeze_late_weekly_rows.py > out_freeze_late.txt 2>&1
"""
from db.database import get_conn

conn = get_conn("db/tracker.db")
Q = """FROM projections p
       WHERE p.scope='weekly' AND p.is_current=1 AND p.is_frozen=0
         AND EXISTS (SELECT 1 FROM weekly_projection_locks l
                     WHERE l.week_id=p.week_id
                       AND l.source_name = REPLACE(p.source_name, ' (live)', ''))"""
print("Before:", [tuple(r) for r in conn.execute("SELECT p.source_name, p.week_id, COUNT(*) " + Q + " GROUP BY 1,2")])
cur = conn.execute("UPDATE projections SET is_frozen=1 WHERE projection_id IN (SELECT p.projection_id " + Q + ")")
conn.commit()
print("Frozen:", cur.rowcount)
print("Remaining unfrozen current weekly rows:",
      [tuple(r) for r in conn.execute("SELECT source_name, week_id, COUNT(*) FROM projections WHERE scope='weekly' AND is_current=1 AND is_frozen=0 GROUP BY 1,2")])
conn.close()
