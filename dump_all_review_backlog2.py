"""
Follow-up to dump_all_review_backlog.py -- that script only joined
entity_type='proposed_observation' and found just 16 rows, but the
fix_and_resolve script's own count (no entity_type filter) reported 229
pending review_items total. This dumps the full breakdown by entity_type
and reason to find the other ~213.

Run from the repo root:
    python dump_all_review_backlog2.py
"""
from db.database import get_conn

DB_PATH = "db/tracker.db"


def main():
    conn = get_conn(DB_PATH)

    print("=== Pending review_items by entity_type ===")
    rows = conn.execute(
        "SELECT entity_type, COUNT(*) c FROM review_items WHERE status='pending' GROUP BY entity_type"
    ).fetchall()
    for r in rows:
        print(f"  {r['entity_type']:25s} {r['c']}")

    print()
    print("=== Pending review_items by entity_type + reason ===")
    rows = conn.execute(
        "SELECT entity_type, reason, COUNT(*) c FROM review_items WHERE status='pending' GROUP BY entity_type, reason ORDER BY c DESC"
    ).fetchall()
    for r in rows:
        print(f"  {r['entity_type']:25s} {r['reason']:25s} {r['c']}")

    print()
    print("=== Sample of 10 non-proposed_observation pending items (full row) ===")
    rows = conn.execute(
        "SELECT * FROM review_items WHERE status='pending' AND entity_type != 'proposed_observation' LIMIT 10"
    ).fetchall()
    for r in rows:
        print(" ", dict(r))

    conn.close()


if __name__ == "__main__":
    main()
