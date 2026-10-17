"""
One-off: break down the full pending review_items backlog (229 items as of
2026-09-24) by source, reason, and age, so Justin can see whether this is
old known backlog or something new/missed.

Run from the repo root:
    python dump_all_review_backlog.py
"""
from db.database import get_conn

DB_PATH = "db/tracker.db"


def main():
    conn = get_conn(DB_PATH)

    print("=== By source_name + reason ===")
    rows = conn.execute(
        """SELECT po.source_name, ri.reason, COUNT(*) c
           FROM review_items ri
           JOIN proposed_observations po ON po.proposed_id = ri.entity_id AND ri.entity_type='proposed_observation'
           WHERE ri.status='pending'
           GROUP BY po.source_name, ri.reason
           ORDER BY po.source_name, c DESC"""
    ).fetchall()
    for r in rows:
        print(f"  {r['source_name']:12s} {r['reason']:20s} {r['c']}")

    print()
    print("=== By created_at date (proposed_observations.import batch time) ===")
    rows = conn.execute(
        """SELECT date(po.created_at) AS d, po.source_name, COUNT(*) c
           FROM review_items ri
           JOIN proposed_observations po ON po.proposed_id = ri.entity_id AND ri.entity_type='proposed_observation'
           WHERE ri.status='pending'
           GROUP BY d, po.source_name
           ORDER BY d"""
    ).fetchall()
    for r in rows:
        print(f"  {r['d']}  {r['source_name']:12s} {r['c']}")

    print()
    print("=== Distinct player names, oldest batch first (first 40) ===")
    rows = conn.execute(
        """SELECT po.extracted_player_name, po.source_name, po.position, ri.reason, MIN(po.created_at) AS first_seen, COUNT(*) c
           FROM review_items ri
           JOIN proposed_observations po ON po.proposed_id = ri.entity_id AND ri.entity_type='proposed_observation'
           WHERE ri.status='pending'
           GROUP BY po.extracted_player_name, po.source_name
           ORDER BY first_seen
           LIMIT 40"""
    ).fetchall()
    for r in rows:
        print(f"  {r['first_seen']}  {r['source_name']:10s} {r['extracted_player_name']!r:30s} pos={r['position']} reason={r['reason']} rows={r['c']}")

    conn.close()


if __name__ == "__main__":
    main()
