"""
Yahoo weekly Player List adapter (Week 2, 2026 specimen).

Source shape: Yahoo's "Player List" page (Fantasy > Players > Research),
Stats filter set to "Week <N> (proj)", exported as PDF (25 rows/page,
paginated via "Previous 25 / Next 25"). Justin supplied 17 sequential PDF
exports covering the top 424 offensive players by that week's Fan Pts.

Per the frozen boundary, this file's ONLY job is reading a pre-parsed JSON
payload (produced by a separate, one-off deterministic text-layout parser --
see the accompanying run script for how the payload was built from the raw
PDFs) and emitting NormalizedObservations. Everything else -- identity
resolution, validation, confidence routing, versioning -- is the unchanged
ingestion.pipeline.ingest_collector_batch() -> _route_field() path every
other source goes through.

Known limitations, stated plainly:
- Yahoo's Player List shows Passing Yds/TD/Int only -- no completions or
  pass attempts -- so pass_completions/pass_attempts are never produced for
  QBs (same gap the manual Week 1 transcription already had).
- Targets (Tgt*), Return Yds/TD, and 2PT are shown on the page but have no
  corresponding entry in stat_definitions (Decision 1's approved per-position
  stat list never included them) -- deliberately not emitted, not silently
  dropped-and-hidden: they simply have no home in this schema yet.
- TE rushing: stat_definitions only approved rush_attempts for TE (no
  rush_yards/rush_tds), matching AthleticWeeklyXlsxAdapter's TE block, which
  has no rush stats at all -- so for TE rows this adapter emits
  rush_attempts only, never rush_yards/rush_tds, even when Yahoo shows them.
- Rankings gap this run resolves: Yahoo's page shows two rank columns,
  "Pre-Season" and "Actual", under one identity in the UI -- but rankings
  identity in this schema is (player, week_id, source, ranking_type, scope),
  and both are ranking_type='overall' for the same player/source, so storing
  them naively under the same week/scope would make the second overwrite the
  first as a "conflicting version" of one ranking, which is wrong -- they are
  two genuinely different observations, not two readings of the same thing.
  Resolved without any schema/pipeline change by using the scope dimension
  that already exists: "Pre-Season" -> scope='season' (week_id NULL; it's
  Yahoo's fixed preseason/draft-time overall rank, doesn't change week to
  week) and "Actual" -> scope='weekly' (week_id = this week; it's the
  season-to-date overall rank as of this week). Those are genuinely
  different (week_id, scope) identities, so both persist independently.
- Status tags (Q/O/D/IR/IR-R/NA/PUP/etc.) riding along in Yahoo's own name
  string are stripped before this adapter ever sees the name (done by the
  payload-building step, not here) -- same treatment as the Week 1 review
  queue fix, just applied up front instead of via a post-hoc alias.
"""
import json
from typing import List

from ingestion.collector import SourceAdapter, NormalizedObservation, AdapterUnavailable

_QB_STATS = ["pass_yards", "pass_tds", "interceptions", "rush_attempts", "rush_yards", "rush_tds"]
_RB_WR_STATS = ["rush_attempts", "rush_yards", "rush_tds", "receptions", "receiving_yards",
                "receiving_tds", "fumbles_lost"]
_TE_STATS = ["rush_attempts", "receptions", "receiving_yards", "receiving_tds", "fumbles_lost"]

_STAT_SOURCE_FIELD = {
    "pass_yards": "pass_yards", "pass_tds": "pass_tds", "interceptions": "interceptions",
    "rush_attempts": "rush_attempts", "rush_yards": "rush_yards", "rush_tds": "rush_tds",
    "receptions": "receptions", "receiving_yards": "receiving_yards", "receiving_tds": "receiving_tds",
    "fumbles_lost": "fumbles_lost",
}

_POSITION_STATS = {"QB": _QB_STATS, "RB": _RB_WR_STATS, "WR": _RB_WR_STATS, "TE": _TE_STATS}

_PROJECTION_CONFIDENCE = 0.9
_RANKING_CONFIDENCE = 0.9


class YahooWeeklyPdfAdapter(SourceAdapter):
    """Reads a pre-parsed JSON payload (list of player dicts with clean_name,
    team_abbr, position, and the Yahoo column values) and emits scope='weekly'
    raw-stat projections plus scope='season'/'weekly' overall rankings."""

    def __init__(self, source_name: str, payload_path: str):
        self.source_name = source_name
        self.payload_path = payload_path

    def fetch(self, season_id: int, week_number: int) -> List[NormalizedObservation]:
        try:
            with open(self.payload_path) as f:
                records = json.load(f)
        except FileNotFoundError as e:
            raise AdapterUnavailable(f"Payload file not found: {self.payload_path}", transient=True) from e
        except json.JSONDecodeError as e:
            raise AdapterUnavailable(f"Payload file is not valid JSON: {e}", transient=False) from e

        observations: List[NormalizedObservation] = []
        for r in records:
            name = r["clean_name"]
            position = r["position"]
            team = r.get("team_abbr")

            for stat_name in _POSITION_STATS.get(position, []):
                value = r.get(_STAT_SOURCE_FIELD[stat_name])
                if value is None:
                    continue
                observations.append(NormalizedObservation(
                    player_name_raw=name,
                    position_hint=position,
                    data_type="projection",
                    stat_name=stat_name,
                    value=round(float(value), 2),
                    confidence=_PROJECTION_CONFIDENCE,
                    week_number=week_number,
                    scope="weekly",
                    team_hint=team,
                ))

            pre_season = r.get("pre_season_rank")
            if pre_season is not None:
                observations.append(NormalizedObservation(
                    player_name_raw=name,
                    position_hint=position,
                    data_type="ranking",
                    stat_name="ranking",
                    value=float(pre_season),
                    confidence=_RANKING_CONFIDENCE,
                    week_number=None,
                    scope="season",
                    team_hint=team,
                    ranking_type="overall",
                ))

            actual = r.get("actual_rank")
            if actual is not None:
                observations.append(NormalizedObservation(
                    player_name_raw=name,
                    position_hint=position,
                    data_type="ranking",
                    stat_name="ranking",
                    value=float(actual),
                    confidence=_RANKING_CONFIDENCE,
                    week_number=week_number,
                    scope="weekly",
                    team_hint=team,
                    ranking_type="overall",
                ))

        return observations
