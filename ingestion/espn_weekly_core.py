"""
ESPN weekly live-HTTP parsing logic (kona_player_info endpoint).

Deliberately separate from espn_projections_adapter.py: that module
parses a saved season-long PDF and is untouched by this work. This
module parses live weekly JSON responses and produces this app's
NormalizedObservation shape (ingestion.collector), not the standalone
espn_adapter/ project's dataclasses -- the two adapters share no code
and no data model, only the same real-world source.

Endpoint (see espn_adapter/FINDINGS.md for the full research log this
was originally validated against):
    GET https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/3?view=kona_player_info
    header x-fantasy-filter: {"players": {"filterIds": {"value": [...]}}}
No auth required, confirmed repeatedly against live data.
"""
from __future__ import annotations

from typing import Any, Iterable

from ingestion.collector import NormalizedObservation
from ingestion.espn_weekly_stat_map import (
    STAT_ID_TO_NAME,
    FORBIDDEN_STAT_KEYS,
    PROJECTION_ONLY_STAT_ID_TO_NAME,
)

# defaultPositionId -> position string, matching this app's convention
# (see raw_observations rows already in this DB: {"position": "QB", ...}).
# 1/2/3/4 independently confirmed this session (Allen=1/QB, Gibbs=2/RB,
# Chase=3/WR, McBride=4/TE in a live response). 5/K and 16/D-ST are the
# standard published ESPN convention, not independently re-verified,
# and are out of scope anyway (this app only tracks QB/RB/WR/TE).
_DEFAULT_POSITION_ID_TO_POSITION: dict[int, str] = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
}

# proTeamId -> NFL team abbreviation. Only the four IDs actually
# exercised by this session's real specimens are included here
# (BUF=2 via Allen, CIN=4 via Chase, DET=8 via Gibbs); team_hint is
# non-essential metadata for identity resolution (the pipeline resolves
# players by name), so an incomplete table here is a documented gap,
# not a silent-guess risk -- an unmapped proTeamId simply yields no
# team_hint rather than a wrong one.
_PRO_TEAM_ID_TO_ABBREV: dict[int, str] = {
    2: "BUF",
    4: "CIN",
    8: "DET",
    22: "ARI",
}

STAT_SOURCE_ACTUAL = 0
STAT_SOURCE_PROJECTED = 1
STAT_SPLIT_WEEKLY = 1

_POSITIONS_IN_SCOPE = {"QB", "RB", "WR", "TE"}


class ESPNWeeklySchemaError(RuntimeError):
    """Raised when a raw ESPN player entry doesn't match the shape this
    parser was built and verified against -- includes genuine absence
    (e.g. a bye week) as well as a malformed/unexpected response.
    Callers decide which of those is fatal; this parser never guesses
    or substitutes a default."""


def _get_player_dict(entry: dict[str, Any]) -> dict[str, Any]:
    player = entry.get("player")
    if not isinstance(player, dict):
        raise ESPNWeeklySchemaError(f"player entry missing 'player' object: keys={list(entry.keys())}")
    return player


def _resolve_position(default_position_id: Any, full_name: str) -> str | None:
    """Returns None (not a guess) for anything outside QB/RB/WR/TE --
    the caller skips that player rather than fabricating a position."""
    return _DEFAULT_POSITION_ID_TO_POSITION.get(default_position_id)


def _iter_stats(entry: dict[str, Any]) -> Iterable[dict[str, Any]]:
    player = _get_player_dict(entry)
    stats = player.get("stats")
    if stats is None:
        raise ESPNWeeklySchemaError(
            f"player entry for {player.get('fullName')!r} has no 'stats' array at all "
            "(expected at least an empty list); refusing to treat missing data as zero"
        )
    return stats


def find_weekly_stat_entry(
    entry: dict[str, Any], season_id: int, week_number: int, stat_source_id: int
) -> dict[str, Any] | None:
    """Returns the single stats[] entry matching (seasonId, scoringPeriodId,
    statSourceId, statSplitTypeId=WEEKLY), or None if there isn't exactly
    one (absence -- e.g. bye week, not-yet-played -- is expected and left
    for the caller to skip, never silently substituted with 0)."""
    if week_number <= 0:
        raise ValueError("week_number must be a positive NFL week (1-18)")

    matches = [
        s
        for s in _iter_stats(entry)
        if s.get("seasonId") == season_id
        and s.get("scoringPeriodId") == week_number
        and s.get("statSourceId") == stat_source_id
        and s.get("statSplitTypeId") == STAT_SPLIT_WEEKLY
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def extract_raw_stat_observations(
    stat_entry: dict[str, Any],
    player_name: str,
    position: str,
    team_hint: str | None,
    week_number: int,
    data_type: str,
    confidence: float,
) -> list[NormalizedObservation]:
    """Turns one raw ESPN stat entry's inner `stats` dict into a list of
    NormalizedObservation, one per recognized raw category. appliedTotal
    is never read here regardless of what's in the source payload --
    this project's invariant is that fantasy points are always derived
    downstream (fantasy/scoring.py), never stored as a raw stat."""
    inner = stat_entry.get("stats")
    if not isinstance(inner, dict):
        raise ESPNWeeklySchemaError(f"stat entry for {player_name!r} has no inner 'stats' dict: {stat_entry}")

    observations: list[NormalizedObservation] = []
    for raw_id, raw_value in inner.items():
        if raw_id in FORBIDDEN_STAT_KEYS:
            continue  # defensive; appliedTotal lives one level up in practice, not in this dict
        stat_name = STAT_ID_TO_NAME.get(raw_id)
        if stat_name is None and data_type == "projection":
            stat_name = PROJECTION_ONLY_STAT_ID_TO_NAME.get(raw_id)
        if stat_name is None:
            continue  # unmapped ID -- not one of the 9 locked categories; skip, don't guess
        observations.append(NormalizedObservation(
            player_name_raw=player_name,
            position_hint=position,
            data_type=data_type,
            stat_name=stat_name,
            value=float(raw_value),
            confidence=confidence,
            week_number=week_number,
            team_hint=team_hint,
            scope="weekly",
        ))
    return observations


def parse_player_entry_weekly(
    entry: dict[str, Any], season_id: int, week_number: int, confidence: float
) -> list[NormalizedObservation]:
    """Full pipeline for one raw ESPN player entry -> zero or more
    NormalizedObservations (projection + actual, whichever are present
    and in-scope). Never raises for ordinary absence (bye week, future
    week, out-of-scope position) -- returns []. Raises ESPNWeeklySchemaError
    only for a genuinely malformed entry."""
    player = _get_player_dict(entry)
    full_name = player.get("fullName")
    if not full_name:
        raise ESPNWeeklySchemaError(f"player entry missing fullName: keys={list(player.keys())}")

    position = _resolve_position(player.get("defaultPositionId"), full_name)
    if position not in _POSITIONS_IN_SCOPE:
        return []  # K, D/ST, or unrecognized -- out of scope for this app, not a guess

    team_hint = _PRO_TEAM_ID_TO_ABBREV.get(player.get("proTeamId"))

    observations: list[NormalizedObservation] = []
    for stat_source_id, data_type in (
        (STAT_SOURCE_PROJECTED, "projection"),
        (STAT_SOURCE_ACTUAL, "actual"),
    ):
        stat_entry = find_weekly_stat_entry(entry, season_id, week_number, stat_source_id)
        if stat_entry is None:
            continue  # expected absence (bye week, not yet played, no projection published) -- skip
        observations.extend(extract_raw_stat_observations(
            stat_entry, full_name, position, team_hint, week_number, data_type, confidence,
        ))
    return observations
