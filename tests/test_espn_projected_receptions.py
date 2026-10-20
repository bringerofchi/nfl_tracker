"""
ESPN weekly parsing: projected receptions arrive as raw stat ID 53 (projection-only
mapping); actuals carry receptions as ID 41 and must never double-count via 53.
Inputs are minimal hand-built dicts exercising the parser's mapping rule only; the
real-data evidence for ID 53 is documented in ingestion/espn_weekly_stat_map.py.
"""
import unittest

from ingestion.espn_weekly_core import extract_raw_stat_observations


def _obs(inner, data_type):
    entry = {"stats": inner}
    return extract_raw_stat_observations(entry, "Test Player", "WR", None, 6, data_type, 0.85)


def _by_name(observations):
    out = {}
    for o in observations:
        out.setdefault(o.stat_name, []).append(o.value)
    return out


class EspnProjectedReceptionsTests(unittest.TestCase):
    def test_projection_id_53_maps_to_receptions(self):
        got = _by_name(_obs({"42": 93.5, "43": 0.5, "53": 7.1}, "projection"))
        self.assertEqual(got["receptions"], [7.1])
        self.assertEqual(got["receiving_yards"], [93.5])


    def test_actual_id_53_is_not_mapped_so_receptions_not_double_counted(self):
        got = _by_name(_obs({"41": 6, "42": 100, "53": 6}, "actual"))
        self.assertEqual(got["receptions"], [6.0])  # exactly one, from ID 41


    def test_actual_with_only_id_53_emits_no_receptions(self):
        got = _by_name(_obs({"42": 100, "53": 6}, "actual"))
        self.assertNotIn("receptions", got)


    def test_projection_id_41_still_maps_if_ever_present(self):
        got = _by_name(_obs({"41": 5}, "projection"))
        self.assertEqual(got["receptions"], [5.0])


if __name__ == "__main__":
    unittest.main()
