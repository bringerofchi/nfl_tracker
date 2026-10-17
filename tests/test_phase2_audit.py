"""
Phase 2 audit / Phase 3 readiness tests. These verify the five areas flagged
in the pre-Phase-3 review, at the database layer (not just pure-function level).
"""
import unittest
import tempfile
import os
import sqlite3
from db.database import init_db, get_conn
from db import repository as repo
from fantasy import service as fsvc


class TestCanonicalSourceDeterminism(unittest.TestCase):
    """Area 1: the trusted-actuals source selection must be deterministic,
    not merely 'happens to work with one seeded source'."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        init_db(reset=True, db_path=self.path)
        self.conn = get_conn(self.path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_only_one_source_can_be_canonical_actuals(self):
        # A second source being marked canonical must be impossible at the DB
        # level — this is what makes source selection deterministic rather
        # than dependent on undefined SQLite row ordering.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE sources SET is_canonical_actuals='yes' WHERE name='ESPN'")

    def test_canonical_source_is_nfl_by_default(self):
        row = self.conn.execute("SELECT name FROM sources WHERE is_canonical_actuals='yes'").fetchone()
        self.assertEqual(row["name"], "NFL")


class TestReviewQueueReversion(unittest.TestCase):
    """Area 4 (and the audit's main finding): resolving a conflict in the
    Review Queue must actually change which value is trusted."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.path)
        self.conn = get_conn(self.path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]
        self.pid = repo.create_player(self.conn, "Audit WR", "WR")

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _seed_conflict(self, first=75, second=92):
        repo.add_statistic(self.conn, self.pid, self.season_id, self.week1, "receiving_yards", first,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, self.pid, self.season_id, self.week1, "receiving_yards", second,
                            source_name="NFL", verification_status="verified")
        return self.conn.execute("SELECT review_id FROM review_items WHERE status='pending'").fetchone()["review_id"]

    def test_rejecting_reverts_trust_to_original_value(self):
        review_id = self._seed_conflict(75, 92)
        repo.resolve_statistic_conflict(self.conn, review_id, "rejected")

        trusted = self.conn.execute(
            "SELECT stat_value FROM statistics WHERE player_id=? AND stat_name='receiving_yards' AND is_current=1",
            (self.pid,),
        ).fetchone()
        self.assertEqual(trusted["stat_value"], 75)  # reverted to the original

        # both rows still exist — nothing was deleted
        rows = self.conn.execute(
            "SELECT stat_value, is_current FROM statistics WHERE player_id=? ORDER BY statistic_id", (self.pid,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["stat_value"] for r in rows}, {75, 92})

        # review item is resolved, not left pending
        item = self.conn.execute("SELECT status FROM review_items WHERE review_id=?", (review_id,)).fetchone()
        self.assertEqual(item["status"], "rejected")

        # the reversal itself is auditable in the corrections table
        corr = self.conn.execute(
            "SELECT * FROM corrections WHERE entity_type='statistic' AND field_name='is_current'"
        ).fetchone()
        self.assertIsNotNone(corr)

    def test_rejecting_updates_downstream_fantasy_calculation(self):
        review_id = self._seed_conflict(75, 92)
        before = fsvc.compute_actual_fp(self.conn, self.pid, self.week1, "WR")
        self.assertAlmostEqual(before.total_points, 9.2)  # trusted value was 92

        repo.resolve_statistic_conflict(self.conn, review_id, "rejected")
        after = fsvc.compute_actual_fp(self.conn, self.pid, self.week1, "WR")
        self.assertAlmostEqual(after.total_points, 7.5)  # now reflects the reverted trusted value (75)

    def test_approving_keeps_new_value_trusted(self):
        review_id = self._seed_conflict(75, 92)
        repo.resolve_statistic_conflict(self.conn, review_id, "approved")
        trusted = self.conn.execute(
            "SELECT stat_value, verification_status FROM statistics WHERE player_id=? AND is_current=1", (self.pid,)
        ).fetchone()
        self.assertEqual(trusted["stat_value"], 92)
        self.assertEqual(trusted["verification_status"], "verified")
        item = self.conn.execute("SELECT status FROM review_items WHERE review_id=?", (review_id,)).fetchone()
        self.assertEqual(item["status"], "approved")

    def test_resolving_twice_is_safe_and_does_not_flip_state_back(self):
        review_id = self._seed_conflict(75, 92)
        repo.resolve_statistic_conflict(self.conn, review_id, "rejected")
        trusted_after_first = self.conn.execute(
            "SELECT stat_value FROM statistics WHERE player_id=? AND is_current=1", (self.pid,)
        ).fetchone()["stat_value"]
        self.assertEqual(trusted_after_first, 75)

        # Re-resolving an already-resolved item should not silently re-toggle trust.
        # The function's contract only operates on pending items; calling it again
        # re-applies the same rejection (idempotent), it must not flip back to 92.
        repo.resolve_statistic_conflict(self.conn, review_id, "rejected")
        trusted_after_second = self.conn.execute(
            "SELECT stat_value FROM statistics WHERE player_id=? AND is_current=1", (self.pid,)
        ).fetchone()["stat_value"]
        self.assertEqual(trusted_after_second, 75)


class TestMissingVsZeroAtDatabaseLayer(unittest.TestCase):
    """Area 2: explicitly verify (not assume) that a confirmed zero and an
    explicitly-missing stat are distinguishable through the full DB path,
    not just at the pure calculate_fantasy_points() level."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.path)
        self.conn = get_conn(self.path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_confirmed_zero_scores_zero_not_missing(self):
        pid = repo.create_player(self.conn, "ConfirmedZero WR", "WR")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receiving_tds", 0,
                            source_name="NFL", verification_status="verified")
        vals = fsvc.get_actual_stat_values(self.conn, pid, self.week1, "WR")
        self.assertEqual(vals["receiving_tds"], 0)  # not None

    def test_explicit_missing_value_state_is_not_scored_as_zero(self):
        pid = repo.create_player(self.conn, "ExplicitMissing WR", "WR")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receiving_yards", None,
                            source_name="NFL", value_state="missing", verification_status="unverified")
        vals = fsvc.get_actual_stat_values(self.conn, pid, self.week1, "WR")
        self.assertIsNone(vals["receiving_yards"])

    def test_no_row_at_all_is_also_missing(self):
        pid = repo.create_player(self.conn, "NoRow WR", "WR")
        vals = fsvc.get_actual_stat_values(self.conn, pid, self.week1, "WR")
        self.assertTrue(all(v is None for v in vals.values()))

    def test_partial_stat_line_flagged_incomplete_not_deceptively_low(self):
        pid = repo.create_player(self.conn, "Partial WR", "WR")
        # Only 2 of 6 applicable stats collected so far
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receptions", 6,
                            source_name="NFL", verification_status="verified")
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "receiving_yards", 80,
                            source_name="NFL", verification_status="verified")
        result = fsvc.compute_actual_fp(self.conn, pid, self.week1, "WR")
        self.assertFalse(result.is_complete)
        self.assertEqual(set(result.missing), {"receiving_tds", "rush_yards", "rush_tds", "fumbles_lost"})
        # the partial total (14.0) must be presented as incomplete, not as a final score
        self.assertAlmostEqual(result.total_points, 14.0)


class TestPositionScopedStatIsolation(unittest.TestCase):
    """Area 3: an out-of-position stat (e.g. a WR with a stray pass_yards row)
    must never leak into that position's fantasy calculation."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.season_id = init_db(reset=True, db_path=self.path)
        self.conn = get_conn(self.path)
        self.week1 = self.conn.execute(
            "SELECT week_id FROM weeks WHERE season_id=? AND week_number=1", (self.season_id,)
        ).fetchone()["week_id"]

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_stray_out_of_position_stat_is_ignored(self):
        pid = repo.create_player(self.conn, "Weird WR", "WR")
        # pass_yards is not in stat_definitions for WR — inserting it directly
        # (bypassing the UI's position-scoped form) must not corrupt scoring.
        repo.add_statistic(self.conn, pid, self.season_id, self.week1, "pass_yards", 300,
                            source_name="NFL", verification_status="verified")
        for name, val in [("receptions", 5), ("receiving_yards", 60), ("receiving_tds", 0),
                           ("rush_yards", 0), ("rush_tds", 0), ("fumbles_lost", 0)]:
            repo.add_statistic(self.conn, pid, self.season_id, self.week1, name, val,
                                source_name="NFL", verification_status="verified")
        result = fsvc.compute_actual_fp(self.conn, pid, self.week1, "WR")
        self.assertAlmostEqual(result.total_points, 11.0)  # 5 receptions + 6.0 yards, pass_yards excluded
        self.assertNotIn("pass_yards", result.breakdown)

    def test_te_has_no_passing_or_rushing_yardage_fields_expected(self):
        # TE gained rush_attempts (informational-only, ESPN weekly-live
        # adapter integration) without gaining rush_yards/rush_tds -- TEs
        # essentially never rush for yardage, but the raw attempt count
        # is still a real category the source can report. This test's
        # intent (no passing/rushing *yardage* categories for TE) still
        # holds; only the exact set changed.
        pid = repo.create_player(self.conn, "Blocking TE", "TE")
        defs = fsvc.get_stat_names_for_position(self.conn, "TE")
        self.assertEqual(set(defs), {"receptions", "receiving_yards", "receiving_tds",
                                      "rush_attempts", "fumbles_lost"})
        self.assertNotIn("pass_yards", defs)
        self.assertNotIn("rush_yards", defs)
        self.assertNotIn("rush_tds", defs)

    def test_qb_zero_receptions_never_scored_since_not_a_qb_field(self):
        pid = repo.create_player(self.conn, "Pocket QB", "QB")
        for name, val in [("pass_yards", 275), ("pass_tds", 2), ("interceptions", 0),
                           ("rush_yards", 0), ("rush_tds", 0), ("pass_completions", 20), ("pass_attempts", 24)]:
            repo.add_statistic(self.conn, pid, self.season_id, self.week1, name, val,
                                source_name="NFL", verification_status="verified")
        result = fsvc.compute_actual_fp(self.conn, pid, self.week1, "QB")
        self.assertTrue(result.is_complete)
        self.assertNotIn("receptions", result.breakdown)
        self.assertNotIn("pass_completions", result.breakdown)  # informational only, never scored
        self.assertNotIn("pass_attempts", result.breakdown)


if __name__ == "__main__":
    unittest.main()
