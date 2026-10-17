"""
Read-only check: did ESPN Week 4 actuals land in db/tracker.db?
Writes nothing. Run from the repo root:
    python verify_week4_actuals.py > out_verify_wk4_actuals.txt 2>&1
"""
from db.database import get_conn

SEASON_YEAR = 2026
WEEK_NUMBER = 4

conn = get_conn("db/tracker.db")
season_id = conn.execute("SELECT season_id FROM seasons WHERE year=?", (SEASON_YEAR,)).fetchone()["season_id"]
week = conn.execute(
    "SELECT week_id, status FROM weeks WHERE season_id=? AND week_number=? AND week_type='regular'",
    (season_id, WEEK_NUMBER),
).fetchone()
print(f"Week {WEEK_NUMBER}: week_id={week['week_id']}, status={week['status']}")

rows = conn.execute("""
    SELECT s.value_state, COUNT(*) AS n, COUNT(DISTINCT s.player_id) AS players
    FROM statistics s JOIN sources src ON src.source_id = s.source_id
    WHERE src.name='ESPN' AND s.week_id=?
    GROUP BY s.value_state
""", (week["week_id"],)).fetchall()
print("ESPN Week 4 statistics by value_state:")
for r in rows:
    print(" ", dict(r))
total = conn.execute("""
    SELECT COUNT(DISTINCT s.player_id) AS players, COUNT(*) AS n
    FROM statistics s JOIN sources src ON src.source_id = s.source_id
    WHERE src.name='ESPN' AND s.week_id=?
""", (week["week_id"],)).fetchone()
print(f"ESPN Week 4 statistics total: {total['players']} distinct players, {total['n']} rows")

print("Spot check (Jahmyr Gibbs, Ja'Marr Chase, Josh Allen):")
for r in conn.execute("""
    SELECT p.display_name, s.stat_name, s.stat_value
    FROM statistics s JOIN sources src ON src.source_id = s.source_id
    JOIN players p ON p.player_id = s.player_id
    WHERE src.name='ESPN' AND s.week_id=?
      AND p.display_name IN ('Jahmyr Gibbs', 'Ja''Marr Chase', 'Josh Allen')
    ORDER BY p.display_name, s.stat_name
""", (week["week_id"],)):
    print(" ", dict(r))
conn.close()
