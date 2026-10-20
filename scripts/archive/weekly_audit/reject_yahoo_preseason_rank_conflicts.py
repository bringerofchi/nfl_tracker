"""
Bulk-resolves all pending 'ranking'/'conflict' review items (213 as of
2026-09-24, all Yahoo pre-season overall rank re-assertions that drifted a
few spots from the value stored during Week 2) by REJECTING every one --
Justin's explicit standing decision (2026-09-24): "we never want to
change a preseason rank." Once a pre-season rank is set, it stays frozen
for the season; a source re-sending a slightly different number in a
later week's pull never overwrites it.

Uses the app's own resolve_ranking_conflict(action='rejected') path (not
a raw UPDATE) so the correction is properly logged in the `corrections`
table and the review_items row is marked resolved, not just silently
matched around.

Run from the repo root:
    python reject_yahoo_preseason_rank_conflicts.py
"""
from db.database import get_conn
from db import repository as repo

DB_PATH = "db/tracker.db"


def main():
    conn = get_conn(DB_PATH)

    pending = conn.execute(
        "SELECT review_id FROM review_items WHERE status='pending' AND entity_type='ranking' AND reason='conflict'"
    ).fetchall()

    print(f"Found {len(pending)} pending ranking conflicts to reject.")

    rejected, failed = 0, 0
    for row in pending:
        try:
            repo.resolve_ranking_conflict(conn, row["review_id"], "rejected", resolved_by="justin_standing_decision")
            rejected += 1
        except ValueError as e:
            failed += 1
            print(f"[FAIL] review_id={row['review_id']}: {e}")
    conn.commit()

    print(f"Rejected {rejected}, failed {failed}.")
    remaining = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    print(f"Remaining pending review items (all sources): {remaining}")
    conn.close()


if __name__ == "__main__":
    main()
