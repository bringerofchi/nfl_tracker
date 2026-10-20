"""
Locked ESPN numeric stat-ID -> canonical stat_name mapping for the
kona_player_info live weekly endpoint's per-entry `stats` dict.

Every entry below was verified against real, dated, named NFL games —
not memorized from a public ID table, not guessed from field position.
Two independent methods were used together for every ID:
  (a) the raw ESPN value matches the same category's value in an
      official box score (Pro-Football-Reference and/or ESPN's own box
      score) for the same player/game, and
  (b) running the full set of mapped values for that player/game
      through this app's own PPR formula (fantasy/scoring.py) reproduces
      ESPN's own `appliedTotal` for that entry exactly.

Evidence log (all cross-references were independently sourced, not
derived from each other):

  ID 0  (pass_attempts)   Josh Allen, BUF vs NE, 2025 wk5: raw=31,
                           PFR box score Att=31.
  ID 1  (pass_completions) same game: raw=22, PFR box score Cmp=22.
  ID 3  (passing_yards)   same game: raw=253, PFR box score Yds=253;
                           StatMuse independently reports the same
                           253 yds / 2 TD / 1 INT line for this game.
  ID 4  (passing_tds)     same game: raw=2, PFR box score TD=2.
  ID 20 (interceptions)   same game: raw=1, PFR box score Int=1.
  ID 23 (rush_attempts)   Allen same game: raw=9, PFR Att=9.
                           Jahmyr Gibbs, DET vs NYG, 2025 wk12: raw=15,
                           PFR/AP recap: "15 carries".
  ID 24 (rush_yards)      Allen same game: raw=53, PFR Yds=53.
                           Gibbs wk12: raw=219, AP recap: "219 yards
                           rushing".
  ID 25 (rush_tds)        Gibbs wk12: raw=2, AP recap: "two touchdowns"
                           (69-yd OT run + 49-yd 4th-quarter run).
                           Not present at all in games with 0 rushing
                           TDs (Allen wk5, Gibbs wk5) -- consistent with
                           this endpoint omitting zero-count categories
                           rather than emitting an explicit 0.
  ID 41 (receptions)      Ja'Marr Chase, CIN vs DET, 2025 wk5: raw=6,
                           ESPN box score 6 REC.
                           Gibbs wk12: raw=11, AP recap: "11 catches".
  ID 42 (receiving_yards) Chase wk5: raw=110, ESPN box score 110 YDS.
                           Gibbs wk12: raw=45, AP recap: "45 yards".
  ID 43 (receiving_tds)   Chase wk5: raw=2, ESPN box score 2 TD.
                           Gibbs wk5: raw=1, ESPN play-by-play (20-yd
                           TD reception from Goff).
                           Gibbs wk12: raw=1, AP recap: "another score".

PPR-formula cross-check (fantasy/scoring.py PPR_SCORING), all exact:
  Chase wk5:  6*1 + 110*0.1 + 2*6                      = 29    (appliedTotal 29)
  Gibbs wk5:  2*1 + 33*0.1 + 1*6 + 54*0.1              = 16.7  (appliedTotal 16.7)
  Gibbs wk12: 11*1 + 45*0.1 + 1*6 + 219*0.1 + 2*6      = 55.4  (appliedTotal 55.4)
  Allen wk5:  253*0.04 + 2*4 + 1*(-2) + 53*0.1 + 1*(-2) = 19.42 (appliedTotal 19.42;
              the final -2 is one lost fumble, confirmed via PFR's
              Fmb=1/FL=1 columns for this game -- required for the
              total to reconcile, so its presence is independently
              corroborated even though its own numeric ID isn't pinned
              down here)

NOT covered here, deliberately:
  - appliedTotal itself is NEVER mapped to a stat_name and is NEVER
    emitted as an observation. It is ESPN's own pre-derived PPR total,
    and this project's invariant is that fantasy points are always
    derived downstream (fantasy/scoring.py) from raw stat categories,
    never stored as a raw stat themselves. See espn_weekly_core.py.
  - fumbles_lost' own numeric ID was not isolated (several same-valued
    candidates in the one game examined). The app's STAT_TO_SCORING_KEY
    includes fumbles_lost, but until a specimen disambiguates it, this
    adapter does not emit a fumbles_lost observation -- absence, not a
    guessed key.
  - ID 22 duplicates ID 3's value (passing yards) in the one specimen
    examined and is not used -- flagged as a likely alias, not mapped.
  - Kicker/D-ST stat IDs are entirely out of scope (this app only
    tracks QB/RB/WR/TE, same as the season-PDF adapter).

If ESPN's schema ever produces a value for one of these IDs that is
inconsistent with the category here (e.g. a wildly out-of-range
passing_yards), that is a signal to re-open this evidence pass, not to
patch around it silently.
"""

STAT_ID_TO_NAME: dict[str, str] = {
    "0": "pass_attempts",
    "1": "pass_completions",
    "3": "pass_yards",
    "4": "pass_tds",
    "20": "interceptions",
    "23": "rush_attempts",
    "24": "rush_yards",
    "25": "rush_tds",
    "41": "receptions",
    "42": "receiving_yards",
    "43": "receiving_tds",
}

# Projection-only IDs (statSourceId=1 entries). ESPN's PROJECTED stat dicts carry
# receptions under ID 53 and never under ID 41, so before this mapping every ESPN
# weekly projection was stored with receptions missing (scored as 0 in PPR).
# Evidence (2026-10-19, live Week 6 pull, scripts/diag_espn_projected_stat_ids.py):
#   (a) In ACTUAL entries IDs 41 and 53 co-occur in 184/184 RB/WR/TE entries with
#       identical values, so 53 is the same quantity as the box-score-verified 41.
#   (b) For the 12 highest-projected RB/WR/TE entries, mapped PPR was 1.3-7.3 points
#       below ESPN's own appliedTotal; adding ID 53 as receptions closed every one to
#       within ~0.24 (most within 0.15). Small residuals remain, unexplained.
# Applies to projections ONLY: actuals already carry receptions as ID 41, and mapping
# 53 there too would record every reception twice.
PROJECTION_ONLY_STAT_ID_TO_NAME: dict[str, str] = {
    "53": "receptions",
}

# Never map or emit this ID under any stat_name -- ESPN's own derived
# PPR total, not a raw statistic. Kept explicit (rather than just an
# absence from the dict above) so a future edit adding more IDs can't
# accidentally reintroduce it without tripping over this guard.
FORBIDDEN_STAT_KEYS = {"appliedTotal"}
