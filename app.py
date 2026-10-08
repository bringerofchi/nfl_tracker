"""
NFL Fantasy Data Tracker — Flask app (Phase 1 + Phase 2: data foundation + manual entry)
Local/private app per Decision 6.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, render_template, request, redirect, url_for, flash
from db.database import get_conn, init_db, DB_PATH
from db import repository as repo
from fantasy import service as fsvc
from fantasy.scoring import calculate_fantasy_points
from ingestion import pipeline as ingest_pipeline
from ingestion.extractor import AnthropicVisionExtractor, ExtractorUnavailable
from ingestion.scheduler import Scheduler, ADAPTER_REGISTRY
import os
import json
from werkzeug.utils import secure_filename

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "local-dev-only"
app.jinja_env.filters["from_json"] = json.loads

if not os.path.exists(DB_PATH):
    init_db()


@app.route("/settings/season_start", methods=["POST"])
def set_season_start():
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    try:
        repo.set_season_start_date(conn, season["season_id"], request.form["start_date"])
        flash(f"Season start date set to {request.form['start_date']}. Season-long projections will lock "
              f"automatically on that date and stay fixed for end-of-season comparison against actuals.", "success")
    except ValueError as e:
        flash(f"Could not set start date — {e}", "warning")
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/coverage")
def coverage_dashboard():
    return render_template("coverage.html")


@app.route("/api/coverage")
def api_coverage():
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    season_id = season["season_id"] if season else None

    total_players = conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    total_sources = conn.execute("SELECT COUNT(DISTINCT source_name) c FROM projections").fetchone()["c"]
    total_projection_rows = conn.execute("SELECT COUNT(*) c FROM projections WHERE is_current=1").fetchone()["c"]
    total_actual_rows = conn.execute("SELECT COUNT(*) c FROM statistics WHERE is_current=1").fetchone()["c"]
    current_week = conn.execute(
        "SELECT MAX(week_number) w FROM weeks WHERE season_id=? AND week_id IN "
        "(SELECT DISTINCT week_id FROM projections WHERE scope='weekly' AND is_current=1)", (season_id,)
    ).fetchone()["w"]

    locks_rows = conn.execute(
        """SELECT l.source_name, l.week_id, l.locked_at, w.week_number
           FROM weekly_projection_locks l JOIN weeks w ON w.week_id=l.week_id
           ORDER BY l.locked_at DESC"""
    ).fetchall()
    locks = []
    for l in locks_rows:
        rows_frozen = conn.execute(
            "SELECT COUNT(*) c FROM projections WHERE source_name=? AND week_id=? AND scope='weekly' AND is_current=1 AND is_frozen=1",
            (l["source_name"], l["week_id"]),
        ).fetchone()["c"]
        locks.append({
            "source_name": l["source_name"], "week_number": l["week_number"],
            "locked_at": l["locked_at"], "rows_frozen": rows_frozen,
        })
    locked_set = {(l["source_name"], l["week_number"]) for l in locks}

    source_names = [r["source_name"] for r in conn.execute("SELECT DISTINCT source_name FROM projections ORDER BY source_name")]
    sources = []
    for sn in source_names:
        weeks = []
        season_row = conn.execute(
            "SELECT COUNT(DISTINCT player_id) players, COUNT(*) rows FROM projections "
            "WHERE source_name=? AND scope='season' AND is_current=1", (sn,)
        ).fetchone()
        weeks.append({"scope": "season", "week_number": None,
                      "players": season_row["players"], "rows": season_row["rows"], "locked": False})
        weekly_rows = conn.execute(
            """SELECT w.week_number, COUNT(DISTINCT pr.player_id) players, COUNT(*) rows
               FROM projections pr JOIN weeks w ON w.week_id=pr.week_id
               WHERE pr.source_name=? AND pr.scope='weekly' AND pr.is_current=1
               GROUP BY w.week_number ORDER BY w.week_number""", (sn,)
        ).fetchall()
        for wr in weekly_rows:
            weeks.append({
                "scope": "weekly", "week_number": wr["week_number"],
                "players": wr["players"], "rows": wr["rows"],
                "locked": (sn, wr["week_number"]) in locked_set,
            })
        actual = conn.execute(
            """SELECT COUNT(DISTINCT st.player_id) players, COUNT(*) rows FROM statistics st
               JOIN sources s ON s.source_id=st.source_id WHERE s.name=? AND st.is_current=1""", (sn,)
        ).fetchone()
        sources.append({
            "source_name": sn, "weeks": weeks,
            "actual_players": actual["players"] or 0, "actual_rows": actual["rows"] or 0,
        })

    review_total = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]
    by_reason = [dict(r) for r in conn.execute(
        "SELECT reason, COUNT(*) count FROM review_items WHERE status='pending' GROUP BY reason ORDER BY count DESC"
    ).fetchall()]
    by_source = [dict(r) for r in conn.execute(
        "SELECT source_name, COUNT(*) count FROM review_items WHERE status='pending' GROUP BY source_name ORDER BY count DESC"
    ).fetchall()]
    recent_raw = conn.execute(
        "SELECT * FROM review_items WHERE status='pending' ORDER BY created_at DESC LIMIT 25"
    ).fetchall()
    recent = []
    for r in recent_raw:
        try:
            details = json.loads(r["details_json"]) if r["details_json"] else {}
        except (TypeError, ValueError):
            details = {}
        recent.append({
            "source_name": r["source_name"],
            "player": details.get("extracted_player_name") or details.get("name"),
            "reason": r["reason"],
            "stat_name": details.get("stat_name"),
            "value": details.get("stat_value") if "stat_value" in details else details.get("new_value"),
            "created_at": r["created_at"],
        })

    runs_raw = conn.execute(
        """SELECT cr.*, s.name as source_name, w.week_number
           FROM collection_runs cr JOIN sources s ON s.source_id=cr.source_id
           LEFT JOIN weeks w ON w.week_id=cr.week_id
           ORDER BY cr.started_at DESC LIMIT 30"""
    ).fetchall()
    runs = []
    for r in runs_raw:
        counts = {"auto_accepted": 0, "sent_to_review": 0, "errors": 0}
        if r["notes"]:
            try:
                parsed = json.loads(r["notes"])
                counts["auto_accepted"] = parsed.get("auto_accepted", 0)
                counts["sent_to_review"] = parsed.get("sent_to_review", 0)
                counts["errors"] = parsed.get("errors", 0)
            except (TypeError, ValueError):
                pass
        runs.append({
            "run_id": r["run_id"], "source_name": r["source_name"], "week_number": r["week_number"],
            "status": r["status"], "started_at": r["started_at"],
            "auto_accepted": counts["auto_accepted"], "sent_to_review": counts["sent_to_review"],
            "errors": counts["errors"],
        })

    conn.close()
    return {
        "summary": {
            "total_players": total_players, "total_sources": total_sources,
            "total_projection_rows": total_projection_rows, "total_actual_rows": total_actual_rows,
            "season_year": season["year"] if season else None, "current_week": current_week,
        },
        "sources": sources,
        "locks": locks,
        "review": {"total": review_total, "by_reason": by_reason, "by_source": by_source, "recent": recent},
        "runs": runs,
    }


@app.route("/")
def dashboard():
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    repo.ensure_season_lock(conn, season["season_id"])  # cheap passive check; no-op unless the date has actually arrived
    season = conn.execute("SELECT * FROM seasons WHERE season_id=?", (season["season_id"],)).fetchone()  # re-read in case lock just happened
    weeks = conn.execute("SELECT * FROM weeks WHERE season_id=? ORDER BY week_number", (season["season_id"],)).fetchall()
    selected_week = request.args.get("week", "1")
    week_row = conn.execute(
        "SELECT * FROM weeks WHERE season_id=? AND week_number=?", (season["season_id"], selected_week)
    ).fetchone()

    completeness = {}
    if week_row:
        for source_name in ["NFL", "Yahoo", "ESPN", "The Athletic"]:
            source = conn.execute("SELECT * FROM sources WHERE name=?", (source_name,)).fetchone()
            if source_name == "NFL":
                cnt = conn.execute(
                    "SELECT COUNT(DISTINCT player_id) c FROM statistics WHERE week_id=? AND source_id=? AND is_current=1",
                    (week_row["week_id"], source["source_id"]),
                ).fetchone()["c"]
            else:
                cnt = conn.execute(
                    "SELECT COUNT(DISTINCT player_id) c FROM rankings WHERE week_id=? AND source_id=? AND is_current=1",
                    (week_row["week_id"], source["source_id"]),
                ).fetchone()["c"]
            completeness[source_name] = cnt

        proj_count = conn.execute(
            "SELECT COUNT(DISTINCT player_id) c FROM projections WHERE week_id=? AND is_current=1", (week_row["week_id"],)
        ).fetchone()["c"]
        completeness["Projections"] = proj_count

    total_players = conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    pending_review = conn.execute("SELECT COUNT(*) c FROM review_items WHERE status='pending'").fetchone()["c"]

    fantasy_summary = None
    if week_row:
        fantasy_summary = fsvc.dashboard_fantasy_summary(conn, season["season_id"], week_row["week_id"])

    conn.close()
    return render_template("dashboard.html", season=season, weeks=weeks, selected_week=selected_week,
                            completeness=completeness, total_players=total_players, pending_review=pending_review,
                            fantasy_summary=fantasy_summary)


@app.route("/players", methods=["GET", "POST"])
def players():
    conn = get_conn()
    if request.method == "POST":
        name = request.form["display_name"].strip()
        position = request.form["position"]
        player_id, match_type = repo.resolve_player(conn, name, position)
        if match_type == "ambiguous":
            conn.execute(
                """INSERT INTO review_items (entity_type, reason, confidence, details_json, status)
                   VALUES ('player_identity', 'ambiguous_identity', 'Low', ?, 'pending')""",
                (f'{{"name": "{name}", "position": "{position}"}}',),
            )
            conn.commit()
            flash(f"'{name}' matched multiple existing players — sent to Review Queue instead of guessing.", "warning")
        elif match_type in ("exact", "alias"):
            flash(f"'{name}' already exists as a {position}.", "info")
        else:
            repo.create_player(conn, name, position)
            flash(f"Added player: {name} ({position})", "success")
        conn.close()
        return redirect(url_for("players"))

    all_players = conn.execute("SELECT * FROM players ORDER BY position, display_name").fetchall()
    conn.close()
    return render_template("players.html", players=all_players)


@app.route("/projections", methods=["GET", "POST"])
def projections():
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()

    if request.method == "POST":
        player_id = int(request.form["player_id"])
        week_number = int(request.form["week_number"])
        week = conn.execute("SELECT * FROM weeks WHERE season_id=? AND week_number=?",
                             (season["season_id"], week_number)).fetchone()
        stat_names = request.form.getlist("stat_name")
        stat_values = request.form.getlist("stat_value")
        added = 0
        try:
            for sn, sv in zip(stat_names, stat_values):
                if sv.strip() == "":
                    continue
                repo.add_projection(conn, player_id, season["season_id"], week["week_id"], sn, float(sv),
                                     source_type="user", source_name="User")
                added += 1
            flash(f"Saved {added} projection field(s).", "success")
        except ValueError as e:
            flash(f"Not saved — {e}", "warning")
        conn.close()
        return redirect(url_for("projections", player_id=player_id, week_number=week_number))

    all_players = conn.execute("SELECT * FROM players ORDER BY position, display_name").fetchall()
    selected_player_id = request.args.get("player_id", type=int)
    selected_week = request.args.get("week_number", 1, type=int)
    stat_defs = []
    existing = []
    if selected_player_id:
        player = conn.execute("SELECT * FROM players WHERE player_id=?", (selected_player_id,)).fetchone()
        if player:
            stat_defs = conn.execute(
                "SELECT * FROM stat_definitions WHERE position=? ORDER BY stat_def_id", (player["position"],)
            ).fetchall()
            week = conn.execute("SELECT * FROM weeks WHERE season_id=? AND week_number=?",
                                 (season["season_id"], selected_week)).fetchone()
            existing = conn.execute(
                "SELECT * FROM projections WHERE player_id=? AND week_id=? AND is_current=1",
                (selected_player_id, week["week_id"]),
            ).fetchall()
    existing_map = {r["stat_name"]: r["projected_value"] for r in existing}
    fp_result = None
    if selected_player_id and existing_map:
        fp_result = calculate_fantasy_points(existing_map)
    conn.close()
    return render_template("projections.html", players=all_players, stat_defs=stat_defs,
                            selected_player_id=selected_player_id, selected_week=selected_week,
                            existing_map=existing_map, fp_result=fp_result)


@app.route("/statistics", methods=["GET", "POST"])
def statistics():
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()

    if request.method == "POST":
        player_id = int(request.form["player_id"])
        week_number = int(request.form["week_number"])
        week = conn.execute("SELECT * FROM weeks WHERE season_id=? AND week_number=?",
                             (season["season_id"], week_number)).fetchone()
        stat_names = request.form.getlist("stat_name")
        stat_values = request.form.getlist("stat_value")
        added = 0
        try:
            for sn, sv in zip(stat_names, stat_values):
                if sv.strip() == "":
                    continue
                repo.add_statistic(conn, player_id, season["season_id"], week["week_id"], sn, float(sv),
                                    source_name="NFL", verification_status="verified")
                added += 1
            flash(f"Saved {added} statistic field(s).", "success")
        except ValueError as e:
            flash(f"Not saved — {e}", "warning")
        conn.close()
        return redirect(url_for("statistics", player_id=player_id, week_number=week_number))

    all_players = conn.execute("SELECT * FROM players ORDER BY position, display_name").fetchall()
    selected_player_id = request.args.get("player_id", type=int)
    selected_week = request.args.get("week_number", 1, type=int)
    stat_defs = []
    existing = []
    if selected_player_id:
        player = conn.execute("SELECT * FROM players WHERE player_id=?", (selected_player_id,)).fetchone()
        if player:
            stat_defs = conn.execute(
                "SELECT * FROM stat_definitions WHERE position=? ORDER BY stat_def_id", (player["position"],)
            ).fetchall()
            week = conn.execute("SELECT * FROM weeks WHERE season_id=? AND week_number=?",
                                 (season["season_id"], selected_week)).fetchone()
            existing = conn.execute(
                "SELECT * FROM statistics WHERE player_id=? AND week_id=? AND source_id=(SELECT source_id FROM sources WHERE name='NFL') AND is_current=1",
                (selected_player_id, week["week_id"]),
            ).fetchall()
    existing_map = {r["stat_name"]: r["stat_value"] for r in existing}
    fp_result = None
    if selected_player_id and existing_map:
        fp_result = calculate_fantasy_points(existing_map)
    conn.close()
    return render_template("statistics.html", players=all_players, stat_defs=stat_defs,
                            selected_player_id=selected_player_id, selected_week=selected_week,
                            existing_map=existing_map, fp_result=fp_result)


@app.route("/rankings", methods=["GET", "POST"])
def rankings():
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()

    if request.method == "POST":
        player_id = int(request.form["player_id"])
        week_number = int(request.form["week_number"])
        source_name = request.form["source_name"]
        ranking_type = request.form["ranking_type"]
        ranking_value = float(request.form["ranking_value"])
        week = conn.execute("SELECT * FROM weeks WHERE season_id=? AND week_number=?",
                             (season["season_id"], week_number)).fetchone()
        repo.add_ranking(conn, player_id, season["season_id"], week["week_id"], source_name, ranking_value,
                          ranking_type, source_type="manual", verification_status="verified")
        flash("Ranking saved.", "success")
        conn.close()
        return redirect(url_for("rankings"))

    all_players = conn.execute("SELECT * FROM players ORDER BY position, display_name").fetchall()
    recent = conn.execute(
        """SELECT r.*, p.display_name, s.name as source_name, w.week_number
           FROM rankings r JOIN players p ON p.player_id=r.player_id
           JOIN sources s ON s.source_id=r.source_id
           JOIN weeks w ON w.week_id=r.week_id
           WHERE r.is_current=1 ORDER BY r.created_at DESC LIMIT 25"""
    ).fetchall()
    conn.close()
    return render_template("rankings.html", players=all_players, recent=recent)


@app.route("/players/<int:player_id>/season")
def player_season_comparison(player_id):
    conn = get_conn()
    player = conn.execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()
    if not player:
        conn.close()
        return "Player not found", 404
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    comparison = fsvc.player_season_projection_comparison(conn, player_id, season["season_id"])
    conn.close()
    return render_template("player_season.html", player=player, season=season, comparison=comparison)


@app.route("/players/<int:player_id>")
def player_detail(player_id):
    conn = get_conn()
    player = conn.execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()
    if not player:
        conn.close()
        return "Player not found", 404
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()

    comparison = fsvc.player_weekly_comparison_by_source(conn, player_id, season["season_id"])
    conn.close()
    return render_template("player_detail.html", player=player, season=season, comparison=comparison)


@app.route("/scheduler")
def scheduler_status():
    conn = get_conn()
    scheduler = Scheduler(conn)
    jobs = scheduler.list_status()
    stale = ingest_pipeline.find_stale_collection_runs(conn, stale_minutes=60)
    conn.close()
    return render_template("scheduler.html", jobs=jobs, stale=stale, adapter_types=list(ADAPTER_REGISTRY))


@app.route("/scheduler/create", methods=["POST"])
def scheduler_create_job():
    conn = get_conn()
    try:
        scheduler = Scheduler(conn)
        season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
        scheduler.create_job(
            source_name=request.form["source_name"],
            adapter_type=request.form["adapter_type"],
            adapter_config={"file_path": request.form["file_path"]},
            season_id=season["season_id"],
            week_number=int(request.form["week_number"]),
            interval_minutes=int(request.form["interval_minutes"]),
            max_retries=int(request.form["max_retries"]),
            retry_backoff_minutes=int(request.form["retry_backoff_minutes"]),
        )
        flash("Job created.", "success")
    except ValueError as e:
        flash(f"Could not create job — {e}", "warning")
    conn.close()
    return redirect(url_for("scheduler_status"))


@app.route("/scheduler/<int:job_id>/run_now", methods=["POST"])
def scheduler_run_now(job_id):
    conn = get_conn()
    scheduler = Scheduler(conn)
    job = scheduler.get_job(job_id)
    if not job:
        flash("Job not found.", "warning")
    else:
        outcome = scheduler.run_job(job)
        flash(f"Run outcome: {outcome['outcome']}", "success" if outcome["outcome"] == "success" else "warning")
    conn.close()
    return redirect(url_for("scheduler_status"))


@app.route("/scheduler/<int:job_id>/toggle", methods=["POST"])
def scheduler_toggle_job(job_id):
    conn = get_conn()
    scheduler = Scheduler(conn)
    job = scheduler.get_job(job_id)
    if job:
        scheduler.set_enabled(job_id, not job["enabled"])
    conn.close()
    return redirect(url_for("scheduler_status"))


@app.route("/scheduler/recover_stale", methods=["POST"])
def scheduler_recover_stale():
    conn = get_conn()
    scheduler = Scheduler(conn)
    recovered = scheduler.recover_stale_runs(stale_minutes=60)
    flash(f"Recovered {len(recovered)} stale run(s).", "success")
    conn.close()
    return redirect(url_for("scheduler_status"))


@app.route("/scheduler/run_due", methods=["POST"])
def scheduler_run_due():
    conn = get_conn()
    scheduler = Scheduler(conn)
    results = scheduler.run_due_jobs()
    flash(f"Ran {len(results)} due job(s).", "success")
    conn.close()
    return redirect(url_for("scheduler_status"))


@app.route("/ingest", methods=["GET", "POST"])
def ingest_screenshot():
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()

    if request.method == "POST":
        file = request.files.get("screenshot")
        if not file or file.filename == "":
            flash("No file selected.", "warning")
            conn.close()
            return redirect(url_for("ingest_screenshot"))

        filename = secure_filename(file.filename)
        save_path = os.path.join(UPLOAD_DIR, f"{os.urandom(4).hex()}_{filename}")
        file.save(save_path)

        week_number = int(request.form["week_number"])
        data_type = request.form["data_type"]
        source_name = request.form["source_name"]
        position_hint = request.form.get("position_hint") or None

        try:
            extractor = AnthropicVisionExtractor()
            result = ingest_pipeline.ingest_screenshot(
                conn, extractor, save_path, filename, season["season_id"], week_number, data_type, source_name,
                position_hint=position_hint,
            )
        except Exception as e:
            flash(f"Ingestion failed: {e}", "warning")
            conn.close()
            return redirect(url_for("ingest_screenshot"))

        if result["extraction_failed"]:
            flash(f"Could not extract from this screenshot ({result.get('error')}). "
                  f"The upload was saved and flagged for review — you can still enter its stats manually.", "warning")
        else:
            flash(f"{len(result['auto_accepted'])} field(s) auto-accepted, "
                  f"{len(result['sent_to_review'])} sent to the Review Queue.", "success")
        conn.close()
        return redirect(url_for("review_queue"))

    conn.close()
    return render_template("ingest.html", season=season)


REVIEW_PER_PAGE = 25


def _resolve_one_review_item(conn, item, action, corrected_value=None, player_override=None, new_player_name=None):
    """Shared resolution logic for both single-item and bulk-reject paths.
    Returns (ok: bool, message: str)."""
    if item["entity_type"] == "statistic" and item["reason"] == "conflict":
        repo.resolve_statistic_conflict(conn, item["review_id"], action)
        return True, f"Conflict {action} — trusted statistic value updated accordingly."
    elif item["entity_type"] == "ranking" and item["reason"] == "conflict":
        repo.resolve_ranking_conflict(conn, item["review_id"], action)
        return True, f"Conflict {action} — trusted ranking value updated accordingly."
    elif item["entity_type"] == "proposed_observation":
        details = json.loads(item["details_json"])
        proposed_id = details["proposed_id"]
        try:
            if action == "approved":
                ingest_pipeline.accept_proposed_observation(
                    conn, proposed_id, corrected_value=corrected_value,
                    player_id_override=player_override, new_player_name=new_player_name,
                )
                return True, "Extracted observation accepted and written to trusted statistics."
            else:
                ingest_pipeline.reject_proposed_observation(conn, proposed_id)
                return True, "Extracted observation rejected. No statistic was created."
        except ValueError as e:
            return False, f"Could not resolve — {e}"
    else:
        conn.execute("UPDATE review_items SET status=?, resolved_at=datetime('now'), resolved_by='user' WHERE review_id=?",
                     (action, item["review_id"]))
        conn.commit()
        return True, "Review item resolved."


@app.route("/review")
def review_queue():
    conn = get_conn()
    entity_type = request.args.get("entity_type", "")
    reason = request.args.get("reason", "")
    source_name = request.args.get("source_name", "")
    page = request.args.get("page", 1, type=int)

    where_clauses, params = ["status='pending'"], []
    if entity_type:
        where_clauses.append("entity_type=?")
        params.append(entity_type)
    if reason:
        where_clauses.append("reason=?")
        params.append(reason)
    if source_name:
        where_clauses.append("source_name=?")
        params.append(source_name)
    where_sql = " AND ".join(where_clauses)

    total = conn.execute(f"SELECT COUNT(*) c FROM review_items WHERE {where_sql}", params).fetchone()["c"]
    total_pages = max(1, -(-total // REVIEW_PER_PAGE))  # ceiling division
    page = max(1, min(page, total_pages))
    offset = (page - 1) * REVIEW_PER_PAGE

    items = conn.execute(
        f"SELECT * FROM review_items WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [REVIEW_PER_PAGE, offset],
    ).fetchall()

    # Filter option counts computed over ALL pending items (not the current
    # filtered subset), so the person can see what's actually available to
    # filter by, not just what's on the current page.
    by_source = conn.execute(
        "SELECT source_name, COUNT(*) c FROM review_items WHERE status='pending' GROUP BY source_name ORDER BY c DESC"
    ).fetchall()
    by_reason = conn.execute(
        "SELECT reason, COUNT(*) c FROM review_items WHERE status='pending' GROUP BY reason ORDER BY c DESC"
    ).fetchall()
    by_entity_type = conn.execute(
        "SELECT entity_type, COUNT(*) c FROM review_items WHERE status='pending' GROUP BY entity_type ORDER BY c DESC"
    ).fetchall()

    all_players = conn.execute("SELECT * FROM players ORDER BY position, display_name").fetchall()
    conn.close()
    return render_template("review.html", items=items, all_players=all_players,
                            total=total, page=page, total_pages=total_pages,
                            by_source=by_source, by_reason=by_reason, by_entity_type=by_entity_type,
                            filter_entity_type=entity_type, filter_reason=reason, filter_source_name=source_name)


@app.route("/review/<int:review_id>/resolve", methods=["POST"])
def resolve_review(review_id):
    conn = get_conn()
    action = request.form["action"]  # approved / rejected
    item = conn.execute("SELECT * FROM review_items WHERE review_id=?", (review_id,)).fetchone()
    if item:
        corrected_value = request.form.get("corrected_value")
        corrected_value = float(corrected_value) if corrected_value not in (None, "") else None
        ok, message = _resolve_one_review_item(
            conn, item, action, corrected_value=corrected_value,
            player_override=request.form.get("player_id_override") or None,
            new_player_name=request.form.get("new_player_name") or None,
        )
        flash(message, "success" if ok else "warning")
    conn.close()
    return redirect(url_for("review_queue"))


@app.route("/review/bulk_reject", methods=["POST"])
def bulk_reject_review():
    """Safe bulk action: reject only. Never bulk-accept — approving at scale
    would mean trusting hundreds of unverified identities/values sight
    unseen, exactly what this architecture's review step exists to prevent.
    Rejecting is always safe and reversible: nothing is deleted, and
    re-running the same import later re-proposes the observation fresh."""
    conn = get_conn()
    entity_type = request.form.get("entity_type", "")
    reason = request.form.get("reason", "")
    source_name = request.form.get("source_name", "")

    where_clauses, params = ["status='pending'"], []
    if entity_type:
        where_clauses.append("entity_type=?")
        params.append(entity_type)
    if reason:
        where_clauses.append("reason=?")
        params.append(reason)
    if source_name:
        where_clauses.append("source_name=?")
        params.append(source_name)
    if not (entity_type or reason or source_name):
        flash("Select at least one filter before bulk-rejecting — refusing to reject the entire queue unfiltered.", "warning")
        conn.close()
        return redirect(url_for("review_queue"))

    where_sql = " AND ".join(where_clauses)
    items = conn.execute(f"SELECT * FROM review_items WHERE {where_sql}", params).fetchall()
    count = 0
    for item in items:
        ok, _ = _resolve_one_review_item(conn, item, "rejected")
        if ok:
            count += 1
    flash(f"Rejected {count} matching review item(s).", "success")
    conn.close()
    return redirect(url_for("review_queue"))


@app.route("/export/<table>")
def export_csv(table):
    import csv, io, os, sqlite3
    from flask import Response
    allowed = {"players", "statistics", "projections", "rankings", "corrections", "review_items"}
    if table not in allowed:
        return "Not found", 404
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
    conn.close()

    if table == "review_items":
        # 2026-09-27 decision: archival (db/archive.py) moves resolved
        # review_items out of the live db once they're old/closed enough,
        # but a caller asking for review items should keep getting the
        # complete history, not a silently-narrowed live-only slice. Union
        # in archive.db's copy when it exists. Not existing yet (nothing
        # archived) is normal, not an error -- just nothing extra to add.
        archive_path = os.path.join(os.path.dirname(__file__), "db", "archive.db")
        if os.path.exists(archive_path):
            arc_conn = sqlite3.connect(archive_path)
            arc_conn.row_factory = sqlite3.Row
            rows += [dict(r) for r in arc_conn.execute("SELECT * FROM review_items").fetchall()]
            arc_conn.close()
        rows.sort(key=lambda r: r["review_id"])

    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return Response(output.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment;filename={table}.csv"})


@app.route("/export/fantasy_points")
def export_fantasy_points():
    """Derived fantasy points export (spec sec. 15). Adds calculated fields
    alongside player/week/source identity WITHOUT replacing any raw-stat export —
    the raw statistics/projections CSVs above are untouched and remain the
    source of truth."""
    import csv, io
    from flask import Response
    conn = get_conn()
    season = conn.execute("SELECT * FROM seasons ORDER BY year DESC LIMIT 1").fetchone()
    players = conn.execute("SELECT * FROM players ORDER BY position, display_name").fetchall()
    weeks = conn.execute("SELECT * FROM weeks WHERE season_id=? ORDER BY week_number", (season["season_id"],)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["player_id", "display_name", "position", "week_number",
                      "projected_fantasy_points", "actual_fantasy_points",
                      "fantasy_point_difference", "absolute_error",
                      "projection_complete", "actual_complete"])
    for p in players:
        for w in weeks:
            cmp = fsvc.compare_projection_actual(conn, p["player_id"], w["week_id"], p["position"])
            if not (cmp["has_projection"] or cmp["has_actual"]):
                continue
            writer.writerow([p["player_id"], p["display_name"], p["position"], w["week_number"],
                              cmp["projected_fp"], cmp["actual_fp"], cmp["difference"], cmp["absolute_difference"],
                              cmp["projection_complete"], cmp["actual_complete"]])
    conn.close()
    return Response(output.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": "attachment;filename=fantasy_points.csv"})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
