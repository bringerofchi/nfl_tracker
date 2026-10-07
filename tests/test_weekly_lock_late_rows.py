"""
Weekly-lock gap (2026-10-07): new projection rows arriving after a source+week
lock must be stored frozen and tagged, and tagged / provably-post-lock rows must
never enter an accuracy comparison. Also covers the one-off migration.
"""
import datetime
import os
import sqlite3
import tempfile
import unittest

from db.database import init_db, get_conn
from db import repository as repo
from fantasy import service as fsvc
import migrate_freeze_late_weekly_projections as mig

CDT = datetime.timezone(datetime.timedelta(hours=-5))  # fixed tz so tests don't depend on the machine's clock zone
TODAY = datetime.date.today().isoformat()


class _Base(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.db_path)
        self.conn = get_conn(self.db_path)
        self.wk = {r["week_number"]: r["week_id"] for r in
                   self.conn.execute("SELECT week_id, week_number FROM weeks WHERE season_id=?", (self.season_id,))}
        self.rb = repo.create_player(self.conn, "Lock Test RB", "RB")
        self.rb2 = repo.create_player(self.conn, "Late Arrival RB", "RB")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def proj(self, player, week, stat, value, source="ESPN"):
        return repo.add_projection(self.conn, player, self.season_id, self.wk[week], stat, value,
                                   source_name=source, scope="weekly")

    def row(self, projection_id):
        return self.conn.execute("SELECT * FROM projections WHERE projection_id=?", (projection_id,)).fetchone()

    def lock(self, source="ESPN", week=1, today=TODAY):
        return repo.ensure_weekly_projection_lock(self.conn, source, self.wk[week], today=today)


class TestAddProjectionAfterLock(_Base):
    def test_1_new_row_in_locked_source_week_is_frozen_and_tagged(self):
        self.proj(self.rb, 1, "rush_yards", 80)
        self.lock()
        pid, created = self.proj(self.rb2, 1, "rush_yards", 55.5)
        self.assertTrue(created)
        r = self.row(pid)
        self.assertEqual(r["is_frozen"], 1)
        self.assertIn(repo.LATE_AFTER_LOCK_TAG, r["notes"])
        self.assertEqual(r["projected_value"], 55.5)  # value untouched

    def test_1b_existing_notes_are_preserved_when_tagging(self):
        self.lock()
        pid, _ = repo.add_projection(self.conn, self.rb, self.season_id, self.wk[1], "rush_yards", 10,
                                     source_name="ESPN", notes="accepted after review from import 27", scope="weekly")
        n = self.row(pid)["notes"]
        self.assertTrue(n.startswith("accepted after review from import 27"))
        self.assertEqual(n.count(repo.LATE_AFTER_LOCK_TAG), 1)

    def test_1c_live_suffix_source_maps_to_base_lock(self):
        self.lock(source="ESPN")
        pid, _ = self.proj(self.rb, 1, "rush_yards", 12, source="ESPN (live)")
        self.assertEqual(self.row(pid)["is_frozen"], 1)
        self.assertIn(repo.LATE_AFTER_LOCK_TAG, self.row(pid)["notes"])

    def test_2_unlocked_source_or_week_behaves_normally(self):
        self.lock(source="ESPN", week=1)
        # different source, same week -> not locked
        pid_a, _ = self.proj(self.rb, 1, "rush_yards", 10, source="Yahoo")
        # same source, different week -> not locked
        pid_b, _ = self.proj(self.rb, 2, "rush_yards", 10, source="ESPN")
        for pid in (pid_a, pid_b):
            r = self.row(pid)
            self.assertEqual(r["is_frozen"], 0)
            self.assertIsNone(r["notes"])

    def test_2b_rows_before_the_lock_are_not_tagged_and_get_frozen_by_the_lock(self):
        pid, _ = self.proj(self.rb, 1, "rush_yards", 80)
        self.assertEqual(self.row(pid)["is_frozen"], 0)
        self.assertTrue(self.lock())
        r = self.row(pid)
        self.assertEqual(r["is_frozen"], 1)
        self.assertIsNone(r["notes"])  # pre-lock rows are never tagged

    def test_2c_season_scope_and_sourceless_rows_unaffected(self):
        self.lock()
        pid, _ = repo.add_projection(self.conn, self.rb, self.season_id, self.wk[1], "rush_yards", 9,
                                     scope="weekly")  # source_name=None (manual entry)
        self.assertEqual(self.row(pid)["is_frozen"], 0)

    def test_3_existing_frozen_row_lock_behavior_unchanged(self):
        pid, _ = self.proj(self.rb, 1, "rush_yards", 80)
        self.lock()
        with self.assertRaises(ValueError) as cm:
            self.proj(self.rb, 1, "rush_yards", 95)
        self.assertIn("already begun", str(cm.exception))
        # identical re-assertion is still an idempotent no-op, not an error and not a new row
        same_id, created = self.proj(self.rb, 1, "rush_yards", 80)
        self.assertEqual(same_id, pid)
        self.assertFalse(created)
        n = self.conn.execute("SELECT COUNT(*) c FROM projections WHERE player_id=?", (self.rb,)).fetchone()["c"]
        self.assertEqual(n, 1)
        self.assertEqual(self.row(pid)["projected_value"], 80)


class TestComparisonEligibility(_Base):
    def _setup_pre_and_post(self):
        self.proj(self.rb, 1, "rush_yards", 80)                    # pre-lock
        self.lock()
        repo.add_projection(self.conn, self.rb, self.season_id, self.wk[1], "rush_attempts", 18,
                            source_name="ESPN", scope="weekly")    # post-lock (tagged at insert)

    def test_4_post_lock_row_excluded_from_accuracy_comparison(self):
        self._setup_pre_and_post()
        vals = fsvc.get_projection_stat_values(self.conn, self.rb, self.wk[1], "RB", source_name="ESPN")
        self.assertIsNone(vals["rush_attempts"])
        # escape hatch still shows it
        raw = fsvc.get_projection_stat_values(self.conn, self.rb, self.wk[1], "RB", source_name="ESPN",
                                              comparison_only=False)
        self.assertEqual(raw["rush_attempts"], 18)

    def test_4b_player_with_only_late_projection_has_no_comparison_projection(self):
        self.lock()
        repo.add_projection(self.conn, self.rb2, self.season_id, self.wk[1], "rush_yards", 40,
                            source_name="ESPN", scope="weekly")
        repo.add_statistic(self.conn, self.rb2, self.season_id, self.wk[1], "rush_yards", 61,
                           source_name="ESPN")
        cmp = fsvc.compare_projection_actual(self.conn, self.rb2, self.wk[1], "RB", source_name="ESPN")
        self.assertFalse(cmp["has_projection"])
        self.assertIsNone(cmp["difference"])

    def test_5_pre_lock_row_is_eligible_even_when_created_on_lock_day(self):
        self._setup_pre_and_post()  # lock date == today == created_at date of the pre-lock row
        vals = fsvc.get_projection_stat_values(self.conn, self.rb, self.wk[1], "RB", source_name="ESPN")
        self.assertEqual(vals["rush_yards"], 80)

    def test_5b_legacy_untagged_row_created_after_lock_date_is_excluded(self):
        # historical rows (e.g. frozen later by freeze_late_weekly_rows.py) carry no tag;
        # the timestamp rule must still exclude ones provably created after the lock
        pid, _ = self.proj(self.rb, 1, "rush_yards", 80)
        self.lock(today="2026-10-01")
        self.conn.execute("UPDATE projections SET created_at='2026-10-20 12:00:00' WHERE projection_id=?", (pid,))
        self.conn.commit()
        vals = fsvc.get_projection_stat_values(self.conn, self.rb, self.wk[1], "RB", source_name="ESPN")
        self.assertIsNone(vals["rush_yards"])

    def test_6_lock_day_boundary_uses_actual_lock_semantics(self):
        # date-only lock = LOCAL calendar date; created_at is UTC.
        f = lambda created, locked: repo.projection_created_after_lock(created, locked, local_tz=CDT)
        # lock-day, UTC date still the 1st
        self.assertFalse(f("2026-10-01 18:00:00", "2026-10-01"))
        # 8pm local on lock day is already Oct 2 in UTC -- a naive date(created_at) > locked_at
        # would wrongly call this post-lock
        self.assertFalse(f("2026-10-02 01:00:00", "2026-10-01"))
        self.assertGreater("2026-10-02", "2026-10-01")  # documents what the naive comparison would have said
        # 1am local the next day is genuinely after the lock day
        self.assertTrue(f("2026-10-02 06:00:00", "2026-10-01"))
        # full-timestamp lock (UTC): exact instant, strictly-after
        self.assertFalse(f("2026-10-01 20:00:00", "2026-10-01 20:00:01"))
        self.assertFalse(f("2026-10-01 20:00:01", "2026-10-01 20:00:01"))
        self.assertTrue(f("2026-10-01 20:00:02", "2026-10-01 20:00:01"))
        # no lock / no timestamp -> never excluded on timestamp grounds
        self.assertFalse(f("2026-10-09 00:00:00", None))
        self.assertFalse(f(None, "2026-10-01"))

    def test_6b_same_day_post_lock_row_is_excluded_by_tag_even_though_timestamp_cannot_tell(self):
        self._setup_pre_and_post()
        late = self.conn.execute(
            "SELECT created_at, notes FROM projections WHERE stat_name='rush_attempts'").fetchone()
        lock = repo.get_weekly_projection_lock(self.conn, "ESPN", self.wk[1])
        # same calendar day as the lock: timestamp alone says "not provably after"...
        self.assertFalse(repo.projection_created_after_lock(late["created_at"], lock["locked_at"], local_tz=CDT))
        # ...but the insert-time tag still excludes it
        self.assertFalse(repo.is_projection_eligible_for_comparison(late["notes"], late["created_at"],
                                                                    lock["locked_at"], local_tz=CDT))


class TestMigration(_Base):
    def _legacy_gap(self):
        """Simulate the pre-fix state: unfrozen rows in a locked source/week."""
        self.proj(self.rb, 1, "rush_yards", 80)
        self.lock(today="2026-10-01")
        # the simulated lock is dated 10/01, so the genuinely pre-lock row must look like it was
        # captured then (real created_at would be "now", i.e. after that simulated lock date)
        self.conn.execute("UPDATE projections SET created_at='2026-10-01 12:00:00' WHERE player_id=?", (self.rb,))
        for stat, val in (("rush_yards", 33.25), ("rush_attempts", 7.5)):
            self.conn.execute(
                """INSERT INTO projections (player_id, season_id, week_id, scope, stat_name, projected_value,
                                            source_type, source_name, notes, is_frozen, is_current)
                   VALUES (?,?,?,?,?,?,?,?,?,0,1)""",
                (self.rb2, self.season_id, self.wk[1], "weekly", stat, val, "ai_extracted", "ESPN",
                 "accepted after review from import 27"))
        # unfrozen row in an UNLOCKED week must be left alone
        self.proj(self.rb, 2, "rush_yards", 70)
        self.conn.execute("UPDATE weeks SET status='completed' WHERE week_id=?", (self.wk[4],))
        self.conn.commit()

    def test_gap_rows_found_by_lock_state_not_timestamps(self):
        self._legacy_gap()
        rows = mig.find_gap_rows(self.conn)
        self.assertEqual(sorted((r["stat_name"], r["projected_value"]) for r in rows),
                         [("rush_attempts", 7.5), ("rush_yards", 33.25)])  # week 2 row excluded (unlocked)

    def test_apply_freezes_tags_audits_without_touching_values_and_is_idempotent(self):
        self._legacy_gap()
        before_all = {r["projection_id"]: r["projected_value"] for r in
                      self.conn.execute("SELECT projection_id, projected_value FROM projections")}
        rows = mig.find_gap_rows(self.conn)
        result = mig.apply_migration(self.conn, rows)
        self.assertEqual(result["frozen"], 2)
        self.assertEqual(result["status_reverted"], 1)

        after_all = {r["projection_id"]: r["projected_value"] for r in
                     self.conn.execute("SELECT projection_id, projected_value FROM projections")}
        self.assertEqual(before_all, after_all)  # no value changed, nothing deleted

        for r in rows:
            cur = self.row(r["projection_id"])
            self.assertEqual(cur["is_frozen"], 1)
            self.assertIn(repo.LATE_AFTER_LOCK_TAG, cur["notes"])
            self.assertTrue(cur["notes"].startswith("accepted after review from import 27"))
        audit = self.conn.execute(
            "SELECT COUNT(*) c FROM corrections WHERE entity_type='projection' AND field_name='is_frozen'"
        ).fetchone()["c"]
        self.assertEqual(audit, 2)

        # unlocked-week row untouched
        wk2 = self.conn.execute("SELECT is_frozen, notes FROM projections WHERE week_id=?", (self.wk[2],)).fetchone()
        self.assertEqual((wk2["is_frozen"], wk2["notes"]), (0, None))

        # status reverted
        self.assertEqual(self.conn.execute("SELECT status FROM weeks WHERE week_id=?", (self.wk[4],)).fetchone()["status"],
                         "upcoming")

        # the migrated rows are now excluded from comparison, pre-lock row still eligible
        self.assertIsNone(fsvc.get_projection_stat_values(self.conn, self.rb2, self.wk[1], "RB", "ESPN")["rush_yards"])
        self.assertEqual(fsvc.get_projection_stat_values(self.conn, self.rb, self.wk[1], "RB", "ESPN")["rush_yards"], 80)

        # second run: nothing left to do
        self.assertEqual(mig.find_gap_rows(self.conn), [])
        again = mig.apply_migration(self.conn, [])
        self.assertEqual((again["frozen"], again["status_reverted"]), (0, 0))

    def test_apply_rolls_back_if_fingerprint_would_change(self):
        self._legacy_gap()
        rows = mig.find_gap_rows(self.conn)
        original = mig.values_fingerprint
        calls = {"n": 0}

        def flaky(conn):
            calls["n"] += 1
            return original(conn) if calls["n"] == 1 else "tampered"
        mig.values_fingerprint = flaky
        try:
            with self.assertRaises(RuntimeError):
                mig.apply_migration(self.conn, rows)
        finally:
            mig.values_fingerprint = original
        # rolled back: still unfrozen, no audit rows, status unchanged
        self.assertEqual(len(mig.find_gap_rows(self.conn)), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM corrections").fetchone()["c"], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM weeks WHERE week_id=?", (self.wk[4],)).fetchone()["status"],
                         "completed")

    def test_unexpected_rows_flagged_outside_week4_espn_yahoo(self):
        # _legacy_gap creates gap rows in WEEK 1 / ESPN -> outside the Week 4 decision's scope
        self._legacy_gap()
        rows = mig.find_gap_rows(self.conn)
        self.assertEqual(len(mig.unexpected_rows(rows)), 2)

    def test_expected_rows_not_flagged(self):
        self.proj(self.rb, 4, "rush_yards", 50, source="Yahoo")
        self.lock(source="Yahoo", week=4)
        self.conn.execute(
            """INSERT INTO projections (player_id, season_id, week_id, scope, stat_name, projected_value,
                                        source_type, source_name, is_frozen, is_current)
               VALUES (?,?,?,?,?,?,?,?,0,1)""",
            (self.rb2, self.season_id, self.wk[4], "weekly", "rush_yards", 9.0, "ai_extracted", "Yahoo"))
        self.conn.commit()
        rows = mig.find_gap_rows(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(mig.unexpected_rows(rows), [])

    def test_dry_run_plan_is_read_only(self):
        self._legacy_gap()
        ro = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        p = mig.plan(ro)
        self.assertEqual(len(p["rows"]), 2)
        self.assertEqual(p["week_status"]["status"], "completed")
        ro.close()
        self.assertEqual(len(mig.find_gap_rows(self.conn)), 2)  # unchanged


if __name__ == "__main__":
    unittest.main()
