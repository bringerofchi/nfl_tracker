"""
Fantasy analytical layer: sits on top of the existing observation/versioning
architecture (players, statistics, projections, rankings, review_items).

Nothing here stores a persisted "fantasy_points" value. Every function
recalculates from whatever raw statistics are currently trusted/selected
(is_current=1, and for actuals the canonical source), so correcting a raw
stat automatically changes the derived fantasy score the next time it's
computed (spec sec. 16) with no separate versioning system for points.
"""
from typing import Dict, List, Optional
from fantasy.scoring import calculate_fantasy_points, PPR_SCORING, FantasyPointsResult


def get_stat_names_for_position(conn, position: str) -> List[str]:
    rows = conn.execute(
        "SELECT stat_name FROM stat_definitions WHERE position=? ORDER BY stat_def_id", (position,)
    ).fetchall()
    return [r["stat_name"] for r in rows]


def _canonical_actuals_source_id(conn) -> Optional[int]:
    row = conn.execute("SELECT source_id FROM sources WHERE is_canonical_actuals='yes' LIMIT 1").fetchone()
    return row["source_id"] if row else None


def get_projection_stat_values(conn, player_id: int, week_id, position: str,
                                source_name: str = "User", scope: str = "weekly",
                                comparison_only: bool = True) -> Dict[str, Optional[float]]:
    """Current, trusted projected raw stats for this player/week (or player/season
    when scope='season' and week_id=None), one source at a time (spec sec. 12:
    sources are never merged). Missing stat_names -> None.

    "week_id IS ?" rather than "week_id=?": audit finding while adding
    season-scope support here — week_id=None (season-scope) would otherwise
    never match via bare equality, the same NULL-safety class of bug already
    fixed for the repository layer's identity lookups. This function had
    never been extended to actually query season-scope data at all until now.

    comparison_only (2026-10-07 policy): for scope='weekly', rows captured after
    their source+week lock (tagged late-after-lock at insert, or provably created
    after the lock -- see repository.is_projection_eligible_for_comparison) are
    NOT valid pre-game projections and are excluded, so they can never enter a
    projected-vs-actual comparison. Excluded stats read as None, exactly like a
    stat the source never provided. Season-scope rows are unaffected. Pass
    comparison_only=False to see every current row regardless."""
    from db import repository as repo
    defs = get_stat_names_for_position(conn, position)
    rows = conn.execute(
        """SELECT stat_name, projected_value, created_at, notes FROM projections
           WHERE player_id=? AND week_id IS ? AND source_name=? AND scope=? AND is_current=1""",
        (player_id, week_id, source_name, scope),
    ).fetchall()
    if comparison_only and scope == "weekly" and week_id is not None:
        lock = repo.get_weekly_projection_lock(conn, source_name, week_id)
        locked_at = lock["locked_at"] if lock else None
        rows = [r for r in rows
                if repo.is_projection_eligible_for_comparison(r["notes"], r["created_at"], locked_at)]
    found = {r["stat_name"]: r["projected_value"] for r in rows}
    return {name: found.get(name) for name in defs}


def get_actual_stat_values(conn, player_id: int, week_id: int, position: str) -> Dict[str, Optional[float]]:
    """Current, trusted actual raw stats (canonical actuals source only). A row with
    value_state != 'observed' (e.g. explicitly marked 'missing') is treated as None,
    same as no row at all — the missing/zero distinction lives in value_state,
    not in the presence/absence of a row."""
    defs = get_stat_names_for_position(conn, position)
    source_id = _canonical_actuals_source_id(conn)
    if source_id is None:
        return {name: None for name in defs}
    rows = conn.execute(
        """SELECT stat_name, stat_value, value_state FROM statistics
           WHERE player_id=? AND week_id=? AND source_id=? AND is_current=1""",
        (player_id, week_id, source_id),
    ).fetchall()
    found = {}
    for r in rows:
        found[r["stat_name"]] = r["stat_value"] if r["value_state"] == "observed" else None
    return {name: found.get(name) for name in defs}


def compute_projected_fp(conn, player_id: int, week_id, position: str,
                          source_name: str = "User", scoring_config=PPR_SCORING, scope: str = "weekly") -> FantasyPointsResult:
    values = get_projection_stat_values(conn, player_id, week_id, position, source_name, scope=scope)
    return calculate_fantasy_points(values, scoring_config)


def compute_actual_fp(conn, player_id: int, week_id: int, position: str,
                       scoring_config=PPR_SCORING) -> FantasyPointsResult:
    values = get_actual_stat_values(conn, player_id, week_id, position)
    return calculate_fantasy_points(values, scoring_config)


def compare_projection_actual(conn, player_id: int, week_id: int, position: str,
                               source_name: str = "User", scoring_config=PPR_SCORING) -> dict:
    """Projected-vs-actual comparison layer (spec sec. 8). Deliberately exposes
    Difference and Absolute Difference rather than an accuracy percentage,
    which behaves pathologically near a zero projection."""
    proj = compute_projected_fp(conn, player_id, week_id, position, source_name, scoring_config)
    actual = compute_actual_fp(conn, player_id, week_id, position, scoring_config)

    has_any_projection = any(v is not None for v in
                              get_projection_stat_values(conn, player_id, week_id, position, source_name).values())
    has_any_actual = any(v is not None for v in
                          get_actual_stat_values(conn, player_id, week_id, position).values())

    difference = None
    absolute_difference = None
    if has_any_projection and has_any_actual:
        difference = round(actual.total_points - proj.total_points, 2)
        absolute_difference = round(abs(difference), 2)

    return {
        "projected_fp": proj.total_points if has_any_projection else None,
        "actual_fp": actual.total_points if has_any_actual else None,
        "projection_complete": proj.is_complete if has_any_projection else None,
        "actual_complete": actual.is_complete if has_any_actual else None,
        "difference": difference,
        "absolute_difference": absolute_difference,
        "has_projection": has_any_projection,
        "has_actual": has_any_actual,
    }


def get_rankings_for_week(conn, player_id: int, week_id: int) -> List[dict]:
    rows = conn.execute(
        """SELECT r.ranking_value, r.ranking_type, s.name as source_name
           FROM rankings r JOIN sources s ON s.source_id = r.source_id
           WHERE r.player_id=? AND r.week_id=? AND r.is_current=1
           ORDER BY r.ranking_type, s.name""",
        (player_id, week_id),
    ).fetchall()
    return [dict(r) for r in rows]


def player_weekly_view(conn, player_id: int, season_id: int, source_name: str = "User") -> List[dict]:
    """Player weekly view (spec sec. 9/10): Week, Projection, Actual, Difference, Rank(s)."""
    player = conn.execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()
    if not player:
        return []
    weeks = conn.execute(
        "SELECT * FROM weeks WHERE season_id=? ORDER BY week_number", (season_id,)
    ).fetchall()

    result = []
    for w in weeks:
        cmp = compare_projection_actual(conn, player_id, w["week_id"], player["position"], source_name)
        if not (cmp["has_projection"] or cmp["has_actual"]):
            continue  # skip weeks with no data at all, keep the view uncluttered
        rankings = get_rankings_for_week(conn, player_id, w["week_id"])
        result.append({
            "week_number": w["week_number"],
            **cmp,
            "rankings": rankings,
        })
    return result


def player_trend(conn, player_id: int, season_id: int, source_name: str = "User") -> List[dict]:
    """Week-over-week trend (spec sec. 10): Week, Projected FP, Actual FP, Difference."""
    weekly = player_weekly_view(conn, player_id, season_id, source_name)
    return [
        {
            "week_number": w["week_number"],
            "projected_fp": w["projected_fp"],
            "actual_fp": w["actual_fp"],
            "difference": w["difference"],
        }
        for w in weekly
    ]


def get_weekly_projection_sources(conn, player_id: int, season_id: int) -> List[str]:
    """Distinct sources with a current weekly-scope projection for this player/season.
    Mirrors the same source-discovery query player_season_projection_comparison already
    uses for scope='season' -- sources are discovered from real data, never assumed,
    and this is the building block the /players/<id> route was missing (see
    player_weekly_comparison_by_source below)."""
    rows = conn.execute(
        """SELECT DISTINCT source_name FROM projections
           WHERE player_id=? AND season_id=? AND scope='weekly' AND is_current=1
             AND source_name IS NOT NULL""",
        (player_id, season_id),
    ).fetchall()
    return sorted(r["source_name"] for r in rows)


def player_weekly_comparison_by_source(conn, player_id: int, season_id: int) -> dict:
    """Weekly (scope='weekly') projected-vs-actual view, accuracy summary, and trend,
    per source -- the weekly counterpart to player_season_projection_comparison, which
    already does this for scope='season'.

    Wiring gap fix (2026-09-07): player_weekly_view/player_trend/player_accuracy_summary
    already accepted a source_name parameter and were individually correct per source,
    but the /players/<id> Flask route called all three with no source_name, silently
    defaulting to source_name='User' every time -- so ESPN's and Yahoo's weekly
    projection accuracy never appeared on this page at all, even after real ESPN/Yahoo
    weekly projections existed in the database. This assembles every source that
    actually has weekly projections for this player and returns per-source results,
    so the page (and the reporting this project's year-one objective depends on) can
    show them side by side, same "never merge sources" convention used everywhere else.
    """
    sources = get_weekly_projection_sources(conn, player_id, season_id)
    by_source = {
        source_name: {
            "weekly": player_weekly_view(conn, player_id, season_id, source_name),
            "trend": player_trend(conn, player_id, season_id, source_name),
            "accuracy": player_accuracy_summary(conn, player_id, season_id, source_name),
        }
        for source_name in sources
    }
    return {"sources": sources, "by_source": by_source}


def player_accuracy_summary(conn, player_id: int, season_id: int, source_name: str = "User") -> dict:
    """Projection-accuracy summary (spec sec. 11). Only counts weeks where both
    a projection and an actual are present; sample size is always shown."""
    weekly = player_weekly_view(conn, player_id, season_id, source_name)
    comparable = [w for w in weekly if w["difference"] is not None]

    n = len(comparable)
    if n == 0:
        return {"weeks": 0, "avg_projected": None, "avg_actual": None, "avg_difference": None,
                "avg_absolute_error": None, "weeks_over": 0, "weeks_under": 0}

    avg_proj = round(sum(w["projected_fp"] for w in comparable) / n, 2)
    avg_actual = round(sum(w["actual_fp"] for w in comparable) / n, 2)
    avg_diff = round(sum(w["difference"] for w in comparable) / n, 2)
    avg_abs_err = round(sum(w["absolute_difference"] for w in comparable) / n, 2)
    weeks_over = sum(1 for w in comparable if w["difference"] > 0)
    weeks_under = sum(1 for w in comparable if w["difference"] < 0)

    return {
        "weeks": n, "avg_projected": avg_proj, "avg_actual": avg_actual,
        "avg_difference": avg_diff, "avg_absolute_error": avg_abs_err,
        "weeks_over": weeks_over, "weeks_under": weeks_under,
    }


def player_season_projection_comparison(conn, player_id: int, season_id: int, scoring_config=PPR_SCORING) -> dict:
    """The actual point of the multi-source work: every source's current
    season-long projection for this player, side by side, plus each source's
    independently-computed fantasy point total. Never merges sources —
    exposes them as parallel columns so the person can see where sources
    agree or disagree, exactly like the weekly projected-vs-actual view does
    for a single source."""
    player = conn.execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()
    if not player:
        return {"sources": [], "stat_rows": [], "totals": {}}

    rows = conn.execute(
        """SELECT DISTINCT source_name FROM projections
           WHERE player_id=? AND season_id=? AND scope='season' AND is_current=1
           ORDER BY source_name""",
        (player_id, season_id),
    ).fetchall()
    sources = [r["source_name"] for r in rows]
    if not sources:
        return {"sources": [], "stat_rows": [], "totals": {}}

    stat_names = get_stat_names_for_position(conn, player["position"])
    matrix = {}  # stat_name -> {source_name: value}
    for source_name in sources:
        values = get_projection_stat_values(conn, player_id, None, player["position"], source_name, scope="season")
        for stat_name in stat_names:
            matrix.setdefault(stat_name, {})[source_name] = values.get(stat_name)

    stat_rows = [{"stat_name": s, "values": matrix[s]} for s in stat_names]

    totals = {}
    for source_name in sources:
        result = compute_projected_fp(conn, player_id, None, player["position"],
                                       source_name=source_name, scoring_config=scoring_config, scope="season")
        totals[source_name] = {"total_points": result.total_points, "is_complete": result.is_complete,
                                "missing": result.missing}

    return {"sources": sources, "stat_rows": stat_rows, "totals": totals}


def dashboard_fantasy_summary(conn, season_id: int, week_id: int) -> dict:
    """Fantasy summary + data-quality panel for the dashboard (spec sec. 14)."""
    players = conn.execute("SELECT player_id, position FROM players").fetchall()

    with_proj, with_actual, with_both, incomplete_actual = 0, 0, 0, 0
    for p in players:
        proj_vals = get_projection_stat_values(conn, p["player_id"], week_id, p["position"])
        actual_vals = get_actual_stat_values(conn, p["player_id"], week_id, p["position"])
        has_proj = any(v is not None for v in proj_vals.values())
        has_actual = any(v is not None for v in actual_vals.values())
        if has_proj:
            with_proj += 1
        if has_actual:
            with_actual += 1
            actual_result = calculate_fantasy_points(actual_vals)
            if not actual_result.is_complete:
                incomplete_actual += 1
        if has_proj and has_actual:
            with_both += 1

    review_counts = conn.execute(
        """SELECT reason, COUNT(*) c FROM review_items WHERE status='pending' GROUP BY reason"""
    ).fetchall()
    review_by_reason = {r["reason"]: r["c"] for r in review_counts}

    return {
        "players_with_projections": with_proj,
        "players_with_actuals": with_actual,
        "players_with_both": with_both,
        "players_with_incomplete_actuals": incomplete_actual,
        "conflicts": review_by_reason.get("conflict", 0),
        "ambiguous_identity": review_by_reason.get("ambiguous_identity", 0),
        "unresolved_review_items": sum(review_by_reason.values()),
    }
