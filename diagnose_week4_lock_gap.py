"""
READ-ONLY diagnostic -- writes nothing. Answers: which current scope='weekly'
projection rows are NOT frozen even though their source+week has a row in
weekly_projection_locks, and what are the 5 Velus Jones Jr. Week 4 rows?

Run from the repo root:
    python diagnose_week4_lock_gap.py > out_diag_wk4_lock_gap.txt 2>&1
"""
import sqlite3

conn = sqlite3.connect("file:db/tracker.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=== weekly_projection_locks (all rows) ===")
for r in conn.execute("""SELECT l.source_name, w.week_number, l.locked_at
                         FROM weekly_projection_locks l JOIN weeks w ON w.week_id=l.week_id
                         ORDER BY w.week_number, l.source_name"""):
    print(" ", dict(r))

print()
print("=== Current weekly projection rows with is_frozen=0 whose source+week IS locked ===")
print("(source_name with ' (live)' mapped to its base source, same as freeze_late_weekly_rows.py)")
rows = conn.execute("""
    SELECT p.source_name, w.week_number, COUNT(*) n, COUNT(DISTINCT p.player_id) players,
           MIN(date(p.created_at)) first_created, MAX(date(p.created_at)) last_created
    FROM projections p JOIN weeks w ON w.week_id=p.week_id
    WHERE p.scope='weekly' AND p.is_current=1 AND p.is_frozen=0
      AND EXISTS (SELECT 1 FROM weekly_projection_locks l
                  WHERE l.week_id=p.week_id AND l.source_name = REPLACE(p.source_name,' (live)',''))
    GROUP BY 1,2 ORDER BY 2,1""").fetchall()
for r in rows:
    print(" ", dict(r))
if not rows:
    print("  (none)")

print()
print("=== Same rows, but only those created AFTER their lock date (true late arrivals) ===")
rows = conn.execute("""
    SELECT p.source_name, w.week_number, COUNT(*) n, COUNT(DISTINCT p.player_id) players
    FROM projections p JOIN weeks w ON w.week_id=p.week_id
    JOIN weekly_projection_locks l ON l.week_id=p.week_id AND l.source_name = REPLACE(p.source_name,' (live)','')
    WHERE p.scope='weekly' AND p.is_current=1 AND p.is_frozen=0 AND date(p.created_at) > date(l.locked_at)
    GROUP BY 1,2 ORDER BY 2,1""").fetchall()
for r in rows:
    print(" ", dict(r))
if not rows:
    print("  (none)")

print()
print("=== Velus Jones Jr. -- every projection row, any week/source ===")
for r in conn.execute("""
    SELECT pl.display_name, pl.position, w.week_number, p.source_name, p.stat_name, p.projected_value,
           p.is_frozen, p.is_current, p.created_at, p.provenance_ref
    FROM projections p JOIN players pl ON pl.player_id=p.player_id
    LEFT JOIN weeks w ON w.week_id=p.week_id
    WHERE pl.display_name LIKE 'Velus Jones%' ORDER BY w.week_number, p.source_name, p.stat_name"""):
    print(" ", dict(r))

print()
print("=== Week 4 ESPN weekly projections: frozen vs unfrozen, by created date ===")
for r in conn.execute("""
    SELECT date(p.created_at) created, p.is_frozen, COUNT(*) n, COUNT(DISTINCT p.player_id) players
    FROM projections p JOIN weeks w ON w.week_id=p.week_id
    WHERE w.week_number=4 AND p.scope='weekly' AND p.is_current=1 AND p.source_name LIKE 'ESPN%'
    GROUP BY 1,2 ORDER BY 1,2"""):
    print(" ", dict(r))
conn.close()
