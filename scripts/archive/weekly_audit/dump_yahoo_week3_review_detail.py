"""
One-off: dump full review_items detail for the Yahoo Week 3 pending queue,
including the raw details_json, so Claude can see exactly why each item was
flagged (not just the reason bucket).

Run from the repo root:
    python dump_yahoo_week3_review_detail.py
"""
from db.database import get_conn

DB_PATH = "db/tracker.db"


def main():
    conn = get_conn(DB_PATH)
    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position, po.identity_match_type,
                  po.resolved_player_id, ri.review_id, ri.reason, ri.details_json
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND po.source_name='Yahoo'
           ORDER BY po.extracted_player_name"""
    ).fetchall()

    print(f"Total pending rows: {len(pending)}")
    seen = set()
    for r in pending:
        key = (r["extracted_player_name"], r["reason"])
        if key in seen:
            continue
        seen.add(key)
        print()
        print(f"name={r['extracted_player_name']!r} pos={r['position']} reason={r['reason']} "
              f"identity_match_type={r['identity_match_type']} resolved_player_id={r['resolved_player_id']}")
        print(f"  details: {r['details_json']}")
    conn.close()


if __name__ == "__main__":
    main()
