(function () {
  "use strict";

  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const dateBadge = document.getElementById("dateBadge");
  const btnRefresh = document.getElementById("btnRefresh");

  function setStatus(kind, text) {
    statusDot.className = "status-dot" + (kind ? " " + kind : "");
    statusText.textContent = text;
  }

  function fmtInt(n) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString();
  }

  function fmtDate(s) {
    if (!s) return "—";
    return s.replace("T", " ").slice(0, 16);
  }

  // ── TAB SWITCHING ──────────────────────────────────────────
  document.querySelectorAll(".nav-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    });
  });

  // ── SCOPE FILTER CHIPS (Sources tab) ───────────────────────
  let scopeFilter = "all";
  document.querySelectorAll('[data-scope-filter]').forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll('[data-scope-filter]').forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      scopeFilter = chip.dataset.scopeFilter;
      if (window.__coverageData) renderSources(window.__coverageData);
    });
  });

  // ── RENDER: OVERVIEW ────────────────────────────────────────
  function renderOverview(data) {
    document.getElementById("sumPlayers").textContent = fmtInt(data.summary.total_players);
    document.getElementById("sumSources").textContent = fmtInt(data.summary.total_sources);
    document.getElementById("sumProjRows").textContent = fmtInt(data.summary.total_projection_rows);
    document.getElementById("sumActualRows").textContent = fmtInt(data.summary.total_actual_rows);
    document.getElementById("sumLocked").textContent = fmtInt(data.locks.length);
    document.getElementById("sumReview").textContent = fmtInt(data.review.total);
    document.getElementById("sumUpdated").textContent = new Date().toLocaleTimeString();

    // Season progress strip — one chip per week, lit up if any source has weekly data
    const weeksWithData = new Set();
    data.sources.forEach((s) => {
      s.weeks.forEach((w) => { if (w.rows > 0) weeksWithData.add(w.week_number); });
    });
    const progressEl = document.getElementById("seasonProgress");
    progressEl.innerHTML = "";
    for (let wk = 1; wk <= 18; wk++) {
      const chip = document.createElement("div");
      chip.className = "week-chip" + (weeksWithData.has(wk) ? " has-data" : "") + (wk === data.summary.current_week ? " current" : "");
      chip.textContent = "WK " + wk;
      progressEl.appendChild(chip);
    }

    // Glance table
    const tbody = document.getElementById("glanceTableBody");
    tbody.innerHTML = "";
    if (data.sources.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No sources ingested yet</td></tr>';
    } else {
      data.sources.forEach((s) => {
        const season = s.weeks.find((w) => w.scope === "season");
        const w1 = s.weeks.find((w) => w.scope === "weekly" && w.week_number === 1);
        const w2 = s.weeks.find((w) => w.scope === "weekly" && w.week_number === 2);
        const tr = document.createElement("tr");
        tr.innerHTML =
          "<td style='font-family:var(--font-head);font-size:14px;font-weight:600;'>" + s.source_name + "</td>" +
          "<td>" + cellFor(season) + "</td>" +
          "<td>" + cellFor(w1) + "</td>" +
          "<td>" + cellFor(w2) + "</td>" +
          "<td>" + fmtInt(s.actual_rows) + (s.actual_rows ? " rows / " + fmtInt(s.actual_players) + " players" : "") + "</td>";
        tbody.appendChild(tr);
      });
    }
  }

  function cellFor(w) {
    if (!w || w.rows === 0) return '<span class="text-dim">—</span>';
    const lockBadge = w.locked ? '<span class="badge locked">🔒 LOCKED</span>' : '<span class="badge unlocked">UNLOCKED</span>';
    return fmtInt(w.players) + " players / " + fmtInt(w.rows) + " rows &nbsp;" + lockBadge;
  }

  // ── RENDER: SOURCES ─────────────────────────────────────────
  function renderSources(data) {
    const grid = document.getElementById("sourcesGrid");
    grid.innerHTML = "";
    if (data.sources.length === 0) {
      grid.innerHTML = '<div class="empty-row" style="padding:30px;">No sources ingested yet</div>';
      return;
    }
    data.sources.forEach((s) => {
      const weeks = s.weeks.filter((w) => scopeFilter === "all" || w.scope === scopeFilter);
      if (weeks.length === 0) return;
      const card = document.createElement("div");
      card.className = "source-card";
      let rowsHtml = "";
      weeks.forEach((w) => {
        const label = w.scope === "season" ? "Season" : "Week " + w.week_number;
        const lockBadge = w.rows === 0 ? "" : (w.locked ? '<span class="badge locked">🔒</span>' : '<span class="badge unlocked">unlocked</span>');
        rowsHtml +=
          '<div class="source-week-row">' +
            '<span class="source-week-label">' + label + '</span>' +
            '<span class="source-week-count">' + (w.rows ? fmtInt(w.players) + "p / " + fmtInt(w.rows) + "r" : "—") + " " + lockBadge + '</span>' +
          '</div>';
      });
      card.innerHTML =
        '<div class="source-card-head"><span class="source-card-title">' + s.source_name + '</span></div>' +
        rowsHtml;
      grid.appendChild(card);
    });
  }

  // ── RENDER: REVIEW ───────────────────────────────────────────
  function renderReview(data) {
    document.getElementById("kpiReviewTotal").textContent = fmtInt(data.review.total);
    document.getElementById("kpiReviewSources").textContent = data.review.by_source.length;
    document.getElementById("kpiReviewReasons").textContent = data.review.by_reason.length ? data.review.by_reason[0].reason : "—";

    const reasonBody = document.getElementById("reviewReasonBody");
    reasonBody.innerHTML = data.review.by_reason.length
      ? data.review.by_reason.map((r) => "<tr><td>" + r.reason + "</td><td>" + fmtInt(r.count) + "</td></tr>").join("")
      : '<tr class="empty-row"><td colspan="2">Nothing pending</td></tr>';

    const sourceBody = document.getElementById("reviewSourceBody");
    sourceBody.innerHTML = data.review.by_source.length
      ? data.review.by_source.map((r) => "<tr><td>" + (r.source_name || "—") + "</td><td>" + fmtInt(r.count) + "</td></tr>").join("")
      : '<tr class="empty-row"><td colspan="2">Nothing pending</td></tr>';

    const itemsBody = document.getElementById("reviewItemsBody");
    itemsBody.innerHTML = data.review.recent.length
      ? data.review.recent.map((it) =>
          "<tr><td>" + (it.source_name || "—") + "</td><td>" + (it.player || "—") + "</td><td>" + it.reason +
          "</td><td>" + (it.stat_name || "—") + "</td><td>" + (it.value !== null ? it.value : "—") +
          "</td><td>" + fmtDate(it.created_at) + "</td></tr>"
        ).join("")
      : '<tr class="empty-row"><td colspan="6">Nothing pending</td></tr>';
  }

  // ── RENDER: RUNS & LOCKS ──────────────────────────────────────
  function renderRuns(data) {
    const locksBody = document.getElementById("locksTableBody");
    locksBody.innerHTML = data.locks.length
      ? data.locks.map((l) =>
          "<tr><td>" + l.source_name + "</td><td>Week " + l.week_number + "</td><td>" + l.locked_at +
          "</td><td>" + fmtInt(l.rows_frozen) + "</td></tr>"
        ).join("")
      : '<tr class="empty-row"><td colspan="4">No weekly projection sets locked yet</td></tr>';

    const runsBody = document.getElementById("runsTableBody");
    runsBody.innerHTML = data.runs.length
      ? data.runs.map((r) =>
          "<tr><td>#" + r.run_id + "</td><td>" + r.source_name + "</td><td>" + (r.week_number ? "Week " + r.week_number : "—") +
          '</td><td><span class="badge status-' + r.status + '">' + r.status.toUpperCase() + "</span></td><td>" + fmtDate(r.started_at) +
          "</td><td>" + fmtInt(r.auto_accepted) + "</td><td>" + fmtInt(r.sent_to_review) + "</td><td>" + fmtInt(r.errors) + "</td></tr>"
        ).join("")
      : '<tr class="empty-row"><td colspan="8">No collection runs yet</td></tr>';
  }

  // ── FETCH & RENDER ALL ──────────────────────────────────────
  async function loadData() {
    setStatus("", "Loading…");
    btnRefresh.querySelector(".refresh-icon").classList.add("spin");
    try {
      const resp = await fetch("/api/coverage");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      window.__coverageData = data;
      renderOverview(data);
      renderSources(data);
      renderReview(data);
      renderRuns(data);
      setStatus("ok", "Live");
      dateBadge.textContent = data.summary.season_year + " Season";
    } catch (err) {
      setStatus("err", "Error loading data");
      console.error(err);
    } finally {
      btnRefresh.querySelector(".refresh-icon").classList.remove("spin");
    }
  }

  btnRefresh.addEventListener("click", loadData);
  loadData();
})();
