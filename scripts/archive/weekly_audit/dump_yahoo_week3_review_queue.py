"""
One-off: dump the pending Yahoo Week 3 review queue so Claude can identity-
resolve the names (web search, multiple sources) before writing the
resolution script.

Run from the repo root:
    python dump_yahoo_week3_review_queue.py
"""
from db.database import get_conn

DB_PATH = "db/tracker.db"


def main():
    conn = get_conn(DB_PATH)
    pending = conn.execute(
        """SELECT po.proposed_id, po.extracted_player_name, po.position, po.source_name, ri.reason
           FROM proposed_observations po
           JOIN review_items ri ON ri.entity_type='proposed_observation' AND ri.entity_id=po.proposed_id
           WHERE ri.status='pending' AND po.source_name='Yahoo'
           ORDER BY po.extracted_player_name"""
    ).fetchall()

    distinct = {}
    for r in pending:
        distinct.setdefault((r["extracted_player_name"], r["position"]), []).append(r["reason"])

    print(f"Total pending rows: {len(pending)}")
    print(f"Distinct (name, position): {len(distinct)}")
    print()
    for (name, position), reasons in sorted(distinct.items()):
        print(f"{name!r:35s} pos={position:3s} reasons={set(reasons)}")
    conn.close()


if __name__ == "__main__":
    main()
