"""
Season projection locking: season-long projections must become immutable on
the season's real calendar start date, so they remain a stable baseline for
comparison against actual full-season stats later. Proves this is an actual
enforcement mechanism, not just a flag nobody checks (which is what the
pre-existing is_frozen column on weekly projections turned out to be).
"""
import unittest
import tempfile
import os
from db.database import init_db, get_conn
from db import repository as repo


class TestSeasonProjectionLocking(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.pid = repo.create_player(self.conn, "Lock Test QB", "QB")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_no_start_date_never_locks(self):
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        locked = repo.ensure_season_lock(self.conn, self.season_id, today="2099-01-01")
        self.assertFalse(locked)  # no start_date set at all — never locks regardless of date

    def test_before_start_date_not_locked(self):
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        locked = repo.ensure_season_lock(self.conn, self.season_id, today="2026-09-01")
        self.assertFalse(locked)
        row = self.conn.execute("SELECT is_frozen FROM projections WHERE is_current=1").fetchone()
        self.assertEqual(row["is_frozen"], 0)

    def test_on_start_date_locks(self):
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        locked = repo.ensure_season_lock(self.conn, self.season_id, today="2026-09-10")
        self.assertTrue(locked)
        row = self.conn.execute("SELECT is_frozen FROM projections WHERE is_current=1").fetchone()
        self.assertEqual(row["is_frozen"], 1)

    def test_after_start_date_locks(self):
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        locked = repo.ensure_season_lock(self.conn, self.season_id, today="2026-12-01")
        self.assertTrue(locked)

    def test_locking_is_idempotent(self):
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        first = repo.ensure_season_lock(self.conn, self.season_id, today="2026-09-10")
        second = repo.ensure_season_lock(self.conn, self.season_id, today="2026-12-01")
        self.assertTrue(first)
        self.assertFalse(second)  # already locked — does nothing on subsequent calls

    def test_locked_projection_cannot_be_modified(self):
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        repo.ensure_season_lock(self.conn, self.season_id, today="2026-09-10")
        with self.assertRaises(ValueError):
            repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4500, scope="season")

    def test_locked_projection_value_unchanged_after_failed_modification_attempt(self):
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        repo.ensure_season_lock(self.conn, self.season_id, today="2026-09-10")
        try:
            repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4500, scope="season")
        except ValueError:
            pass
        row = self.conn.execute(
            "SELECT projected_value FROM projections WHERE is_current=1 AND player_id=?", (self.pid,)
        ).fetchone()
        self.assertEqual(row["projected_value"], 4200)  # untouched — the write never happened

    def test_identical_reentry_after_lock_is_a_silent_noop_not_an_error(self):
        """A locked projection re-submitted with the SAME value (e.g. a
        harmless re-import after the season already locked) must not raise —
        nothing is actually being modified. Only a genuine attempted change
        to frozen data should raise."""
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        repo.ensure_season_lock(self.conn, self.season_id, today="2026-09-10")
        new_id, was_new = repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        self.assertFalse(was_new)  # no error, no new version — just recognized as unchanged
        rows = self.conn.execute("SELECT COUNT(*) c FROM projections WHERE player_id=?", (self.pid,)).fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_writing_after_season_start_triggers_lock_automatically(self):
        """The lock check happens as a side effect of add_projection itself, not
        just an explicitly-called maintenance job — so a season-scope write that
        arrives on/after the start date correctly locks whatever was already
        current BEFORE evaluating the incoming write."""
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        # No explicit ensure_season_lock call here — passing today= directly to
        # add_projection (an injectable clock, same pattern as the scheduler's
        # `now` parameter) proves the lock check is a genuine side effect of the
        # write path itself, not something only a separate maintenance job does.
        with self.assertRaises(ValueError):
            repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4500,
                                 scope="season", today="2026-12-01")

    def test_weekly_projections_unaffected_by_season_lock(self):
        """Locking only ever touches scope='season' rows — weekly projections
        keep their own independent, per-week is_frozen semantics."""
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        repo.add_projection(self.conn, self.pid, self.season_id, week1, "pass_yards", 250, scope="weekly")
        repo.ensure_season_lock(self.conn, self.season_id, today="2026-12-01")
        row = self.conn.execute(
            "SELECT is_frozen FROM projections WHERE scope='weekly' AND is_current=1"
        ).fetchone()
        self.assertEqual(row["is_frozen"], 0)  # untouched by the season lock
        # still freely editable
        repo.add_projection(self.conn, self.pid, self.season_id, week1, "pass_yards", 275, scope="weekly")

    def test_rankings_unaffected_by_season_lock_scope(self):
        """Only projections lock per this request — rankings are explicitly out
        of scope unless asked for separately."""
        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_ranking(self.conn, self.pid, self.season_id, None, "The Athletic", 3, "QB", scope="season")
        repo.ensure_season_lock(self.conn, self.season_id, today="2026-12-01")
        # still freely editable — locking never touched rankings
        repo.add_ranking(self.conn, self.pid, self.season_id, None, "The Athletic", 5, "QB", scope="season")
        rows = self.conn.execute("SELECT COUNT(*) c FROM rankings WHERE player_id=?", (self.pid,)).fetchone()["c"]
        self.assertEqual(rows, 2)

    def test_set_season_start_date_rejects_bad_format(self):
        with self.assertRaises(ValueError):
            repo.set_season_start_date(self.conn, self.season_id, "09/10/2026")

    def test_full_pipeline_import_after_lock_routes_to_review_not_crash(self):
        """A collector re-run after the season has locked shouldn't crash the
        whole batch — the affected observation should be handled gracefully."""
        import json
        from ingestion.collector import LocalFileSourceAdapter
        from ingestion import pipeline

        repo.set_season_start_date(self.conn, self.season_id, "2026-09-10")
        repo.add_projection(self.conn, self.pid, self.season_id, None, "pass_yards", 4200, scope="season",
                           today="2026-09-01")
        repo.ensure_season_lock(self.conn, self.season_id, today="2026-12-01")

        src_path = tempfile.mktemp(suffix=".json")
        with open(src_path, "w") as f:
            json.dump([{"player_name": "Lock Test QB", "position": "QB", "data_type": "projection",
                        "stat_name": "pass_yards", "value": 4500, "confidence": 0.95, "scope": "season"}], f)
        adapter = LocalFileSourceAdapter("The Athletic", src_path)
        try:
            result = pipeline.ingest_collector_batch(self.conn, adapter, self.season_id, week_number=1)
            # auto-accept path calls repo.add_projection internally; a ValueError there
            # must not crash the whole batch — _route_field should surface it as an error,
            # not propagate an unhandled exception.
            self.assertFalse(result.get("fetch_failed"))
        finally:
            os.remove(src_path)


if __name__ == "__main__":
    unittest.main()
