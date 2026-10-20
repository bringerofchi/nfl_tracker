"""
Read-only: summarize ALL pending review items so Claude can identity-resolve new
names before writing a resolution script. Week-agnostic (replaces the per-week
dump_weekN_review_queue.py copies, now in scripts/archive/weekly_audit/).

Run from anywhere (it finds the repo root itself):
    python scripts/dump_review_queue.py > out_review_queue.txt 2>&1
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from db.database import get_conn  # noqa: E402

DB_PATH = os.path.join(REPO_ROOT, "db", "tracker.db")


def main():
    conn = get_conn(DB_PATH)
    print("=== Pending by (entity_type, reason) ===")
    for r in conn.execute(
        "SELECT entity_type, reason, COUNT(*) c FROM review_items WHERE status='pending' GROUP BY 1,2 ORDER BY 3 DESC"
    ).fetchall():
        print(f"  {r['entity_type']:22s} {r['reason']:22s} {r['c']}")

    print()
    print("=== proposed_observation items: distinct (name, pos, source, data_type) with detail ===")
    rows = conn.execute(
        """SELECT po.extracted_player_name, po.position, po.source_name, po.data_type, po.identity_match_type,
                  po.resolved_player_id, ri.reason, ri.details_json, COUNT(*) n
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending'
           GROUP BY po.extracted_player_name, po.position, po.source_name, po.data_type, ri.reason
           ORDER BY po.extracted_player_name"""
    ).fetchall()
    for r in rows:
        print(f"{r['extracted_player_name']!r} pos={r['position']} src={r['source_name']} type={r['data_type']} "
              f"reason={r['reason']} match={r['identity_match_type']} resolved_pid={r['resolved_player_id']} rows={r['n']}")
        print(f"    details: {r['details_json']}")

    print()
    print("=== ranking items (sample of 8) ===")
    for r in conn.execute(
        "SELECT review_id, reason, details_json FROM review_items WHERE status='pending' AND entity_type='ranking' LIMIT 8"
    ).fetchall():
        print(f"  review_id={r['review_id']} reason={r['reason']} details={r['details_json']}")
    conn.close()


if __name__ == "__main__":
    main()
