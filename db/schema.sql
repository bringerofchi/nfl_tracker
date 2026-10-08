-- NFL Fantasy Data Tracker — Database Schema
-- Implements spec sections 6-30 (Data Foundation)
-- SQLite. Raw/source observations are kept distinct from derived data.

PRAGMA foreign_keys = ON;

-- ============================================================
-- REFERENCE / DIMENSION TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS sources (
    source_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,          -- 'ESPN', 'Yahoo', 'The Athletic', 'NFL', 'User'
    source_type     TEXT NOT NULL,                  -- 'ranking_provider','stat_provider','user','ai_extraction'
    is_canonical_actuals TEXT NOT NULL DEFAULT 'no', -- 'yes' marks the canonical source for actual NFL stats (sec. 13)
    config_json     TEXT,                           -- adapter-specific config, nullable
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seasons (
    season_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    year            INTEGER NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'active',  -- active, completed
    start_date      TEXT,                             -- real calendar date Week 1 begins; NULL until explicitly set.
                                                        -- Drives season-projection locking below — never guessed/assumed.
    projections_locked_at TEXT,                        -- NULL until locking has happened; set once, idempotent guard
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Stable season/week identifier independent of calendar weeks (sec. 10)
CREATE TABLE IF NOT EXISTS weeks (
    week_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id),
    week_number     INTEGER NOT NULL,                -- 1-18 regular season; playoff weeks handled via week_type
    week_type       TEXT NOT NULL DEFAULT 'regular',  -- 'regular','wildcard','divisional','conference','superbowl'
    start_date      TEXT,
    end_date        TEXT,
    status          TEXT NOT NULL DEFAULT 'upcoming', -- upcoming, in_progress, completed
    UNIQUE(season_id, week_number, week_type)
);

CREATE TABLE IF NOT EXISTS teams (
    team_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    abbreviation    TEXT NOT NULL UNIQUE
);

-- ============================================================
-- PLAYERS & IDENTITY
-- ============================================================

CREATE TABLE IF NOT EXISTS players (
    player_id       INTEGER PRIMARY KEY AUTOINCREMENT,   -- stable internal ID (sec. 8)
    display_name    TEXT NOT NULL,
    normalized_name TEXT NOT NULL,                       -- lowercased, punctuation-stripped, for matching
    position        TEXT NOT NULL,                       -- QB, RB, WR, TE
    external_ids_json TEXT,                               -- {"espn_id":..., "yahoo_id":..., "gsis_id":...}
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_players_normalized_name ON players(normalized_name);

-- Invariant: at most one source may be the canonical actuals source at a time.
-- Fantasy-point calculation selects the trusted actuals source via
-- is_canonical_actuals='yes'; if more than one source could hold that flag,
-- selection would become non-deterministic (SQLite gives no ordering
-- guarantee without ORDER BY). This partial unique index makes that state
-- unrepresentable at the database level rather than relying on application
-- discipline (audit finding, see fantasy layer readiness review).
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_canonical_actuals_source
    ON sources(is_canonical_actuals) WHERE is_canonical_actuals = 'yes';

-- Alternate names/spellings/OCR variants that resolve to a player (sec. 8)
CREATE TABLE IF NOT EXISTS player_name_aliases (
    alias_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    alias           TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    alias_source    TEXT,                                 -- how this alias was learned (ocr, manual, import)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alias_normalized ON player_name_aliases(normalized_alias);

-- Historical team affiliation, NOT a single "current team" field (sec. 9)
CREATE TABLE IF NOT EXISTS player_team_history (
    history_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id),
    week_start      INTEGER NOT NULL,                     -- week_number affiliation begins
    week_end        INTEGER,                               -- NULL = still on team as of last known data
    source          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- GAMES & PARTICIPATION
-- ============================================================

CREATE TABLE IF NOT EXISTS games (
    game_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id),
    week_id         INTEGER NOT NULL REFERENCES weeks(week_id),
    home_team_id    INTEGER REFERENCES teams(team_id),
    away_team_id    INTEGER REFERENCES teams(team_id),
    game_date       TEXT,
    status          TEXT NOT NULL DEFAULT 'scheduled'      -- scheduled, in_progress, final, postponed
);

CREATE TABLE IF NOT EXISTS player_game_participation (
    participation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    game_id         INTEGER NOT NULL REFERENCES games(game_id),
    team_id         INTEGER NOT NULL REFERENCES teams(team_id),
    status          TEXT,                                   -- active, inactive, dnp, injured_reserve
    UNIQUE(player_id, game_id)
);

-- ============================================================
-- RAW / SOURCE OBSERVATIONS  (statistics, projections, rankings)
-- Raw observations are preserved independently of any derived value (sec. 5, 13)
-- ============================================================

CREATE TABLE IF NOT EXISTS statistics (
    statistic_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id),
    week_id         INTEGER NOT NULL REFERENCES weeks(week_id),
    game_id         INTEGER REFERENCES games(game_id),
    team_id         INTEGER REFERENCES teams(team_id),
    stat_name       TEXT NOT NULL,                          -- arbitrary, not hard-coded (sec. 12): 'pass_yards','rush_tds',...
    stat_value      REAL,                                   -- NULL = missing/unavailable, distinct from 0 (sec. 19)
    value_state     TEXT NOT NULL DEFAULT 'observed',        -- observed, missing, not_applicable, dnp, source_unavailable, needs_review
    source_id       INTEGER NOT NULL REFERENCES sources(source_id),
    observation_timestamp TEXT,
    collection_timestamp  TEXT NOT NULL DEFAULT (datetime('now')),
    verification_status   TEXT NOT NULL DEFAULT 'unverified', -- unverified, verified, conflicted, superseded
    observation_version    INTEGER NOT NULL DEFAULT 1,
    provenance_ref  TEXT,                                    -- link to raw_observations.raw_id or free text
    collection_run_id INTEGER REFERENCES collection_runs(run_id),
    is_current      INTEGER NOT NULL DEFAULT 1,               -- 0 once superseded by a newer version (history preserved, never deleted)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stat_identity ON statistics(player_id, season_id, week_id, stat_name, source_id);

CREATE TABLE IF NOT EXISTS projections (
    projection_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id),
    week_id         INTEGER REFERENCES weeks(week_id),         -- nullable: NULL means scope='season' (no single week applies)
    scope           TEXT NOT NULL DEFAULT 'weekly',            -- 'weekly' (week_id required) or 'season' (week_id NULL) —
                                                                 -- season-long/preseason extension. A real structured field,
                                                                 -- same principle as ranking_type: never inferred from week_id
                                                                 -- being NULL alone, always set explicitly.
    stat_name       TEXT NOT NULL,
    projected_value REAL,
    source_type     TEXT NOT NULL DEFAULT 'user',            -- user, ai_extracted, imported
    source_name     TEXT,
    collection_timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    observation_timestamp  TEXT,
    verification_status    TEXT NOT NULL DEFAULT 'unverified',
    notes           TEXT,
    provenance_ref  TEXT,                                      -- audit finding (4A->4B review): statistics had this,
                                                                 -- projections didn't, leaving import/collection_run
                                                                 -- traceability for projections only in free-text notes.
    observation_version    INTEGER NOT NULL DEFAULT 1,
    is_frozen       INTEGER NOT NULL DEFAULT 0,               -- 1 once game/week has begun (sec. 15)
    superseded_by   INTEGER REFERENCES projections(projection_id), -- points to the correcting record, original is preserved
    is_current      INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_projection_identity ON projections(player_id, season_id, week_id, stat_name);

CREATE TABLE IF NOT EXISTS rankings (
    ranking_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL REFERENCES players(player_id),
    season_id       INTEGER NOT NULL REFERENCES seasons(season_id),
    week_id         INTEGER REFERENCES weeks(week_id),          -- nullable: NULL means scope='season' (preseason/draft ranks)
    scope           TEXT NOT NULL DEFAULT 'weekly',              -- 'weekly' or 'season' — same season-long extension as projections
    source_id       INTEGER NOT NULL REFERENCES sources(source_id),
    ranking_value   REAL,
    ranking_type    TEXT NOT NULL,                            -- overall, QB, RB, WR, TE, FLEX, other (sec. 17)
    position        TEXT,
    publication_timestamp TEXT,
    collection_timestamp  TEXT NOT NULL DEFAULT (datetime('now')),
    source_type     TEXT NOT NULL DEFAULT 'automated',         -- automated, manual, ai_extracted
    verification_status    TEXT NOT NULL DEFAULT 'unverified',
    notes           TEXT,
    observation_version     INTEGER NOT NULL DEFAULT 1,
    collection_run_id INTEGER REFERENCES collection_runs(run_id),
    provenance_ref  TEXT,                                      -- Phase 4C prerequisite: parity with statistics/projections
    is_current      INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ranking_identity ON rankings(player_id, season_id, week_id, source_id, ranking_type, scope);

-- ============================================================
-- COLLECTION / IMPORT / PROVENANCE INFRASTRUCTURE
-- ============================================================

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(source_id),
    season_id       INTEGER REFERENCES seasons(season_id),
    week_id         INTEGER REFERENCES weeks(week_id),
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'started',           -- started, successful, partial, failed, error, stale
    failure_type    TEXT,                                       -- 'transient' | 'permanent' | NULL. Set only on
                                                                  -- status='failed' (source outage/malformed source
                                                                  -- payload). Phase 4B's retry policy reads this to
                                                                  -- distinguish "worth retrying soon" from "will not
                                                                  -- fix itself by retrying" (audit finding, 4B prep).
    collector_version TEXT,
    notes           TEXT
);
-- Run-locking (Phase 4B): at most one 'started' run may exist per
-- (source, season, week) at a time. Enforced at the DB level rather than an
-- application-level check, for the same TOCTOU reasons as
-- idx_unique_pending_proposal — two schedulers/processes racing to start the
-- same job must not both succeed. A second INSERT attempting to start an
-- already-active run raises sqlite3.IntegrityError, which
-- ingest_collector_batch converts into a graceful "run_locked" result.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_collection_run
    ON collection_runs(source_id, season_id, week_id) WHERE status = 'started';

CREATE TABLE IF NOT EXISTS imports (
    import_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name       TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    file_hash       TEXT NOT NULL,
    file_type       TEXT NOT NULL,                             -- screenshot, image, pdf, csv, xlsx
    upload_timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    extraction_timestamp TEXT,
    extraction_method TEXT,                                     -- e.g. 'ai_vision_v1'
    status          TEXT NOT NULL DEFAULT 'uploaded'             -- uploaded, extracting, extracted, review, approved, failed
);

-- Original artifact / raw response preserved before normalization (sec. 21)
CREATE TABLE IF NOT EXISTS raw_observations (
    raw_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id       INTEGER REFERENCES imports(import_id),
    collection_run_id INTEGER REFERENCES collection_runs(run_id),
    content_type    TEXT NOT NULL,                              -- 'raw_html','raw_json','ai_extraction_result','file'
    raw_content     TEXT,                                       -- inline text/json payload
    file_path       TEXT,                                       -- for binary artifacts
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Anything needing human attention: low confidence, ambiguous identity, conflicts, duplicates (sec. 28)
CREATE TABLE IF NOT EXISTS review_items (
    review_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL,                              -- 'statistic','projection','ranking','player_identity'
    entity_id       INTEGER,                                     -- id in the relevant table, nullable if not yet created
    reason          TEXT NOT NULL,                               -- low_confidence, ambiguous_identity, conflict, duplicate_candidate, missing_field, source_problem
    confidence      TEXT,                                        -- High, Medium, Low
    source_name     TEXT,                                         -- denormalized from details_json for SQL-level filtering at
                                                                    -- scale (audit finding: filtering thousands of pending items
                                                                    -- by source otherwise requires scanning JSON text per row)
    details_json    TEXT,                                        -- candidate values, field-level confidences, etc.
    status          TEXT NOT NULL DEFAULT 'pending',              -- pending, approved, rejected
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    resolved_by     TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_items_filter ON review_items(status, entity_type, reason, source_name);

-- Full audit trail of every correction to any source-derived value (sec. 29)
CREATE TABLE IF NOT EXISTS corrections (
    correction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL,                               -- 'statistic','projection','ranking','player'
    entity_id       INTEGER NOT NULL,
    field_name      TEXT NOT NULL,
    original_value  TEXT,
    corrected_value TEXT,
    corrected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    corrected_by    TEXT NOT NULL DEFAULT 'user',
    reason          TEXT
);

-- Per-position projection statistic definitions (user-approved, from Decision 1)
CREATE TABLE IF NOT EXISTS stat_definitions (
    stat_def_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    position        TEXT NOT NULL,                               -- QB, RB, WR, TE
    stat_name       TEXT NOT NULL,                                -- machine key, e.g. 'pass_yards'
    display_label   TEXT NOT NULL,                                -- 'Pass Yards'
    UNIQUE(position, stat_name)
);

-- ============================================================
-- PHASE 3: INGESTION PIPELINE (screenshot/AI extraction)
--
-- Architectural point: an extracted field is NEVER written directly into
-- `statistics`. It first becomes a `proposed_observations` row (one per
-- extracted stat field, carrying its own confidence). Only when a proposed
-- observation is auto-accepted (high confidence + resolved identity + valid)
-- or approved by a human in the Review Queue does it get promoted into the
-- existing `statistics` table via the same repo.add_statistic() path manual
-- entry uses — so it inherits the same versioning/conflict-detection for
-- free rather than needing a parallel system.
--
-- This table is intentionally the ONLY new table for Phase 3: `imports` and
-- `raw_observations` already existed (built in Phase 1 in anticipation of
-- this), and the Review Queue (`review_items`) already existed too.
-- ============================================================

CREATE TABLE IF NOT EXISTS proposed_observations (
    proposed_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id           INTEGER NOT NULL REFERENCES imports(import_id),
    raw_id               INTEGER REFERENCES raw_observations(raw_id),
    extracted_player_name TEXT NOT NULL,          -- name as read off the screenshot, verbatim
    resolved_player_id   INTEGER REFERENCES players(player_id),  -- NULL until identity is resolved
    identity_match_type  TEXT,                     -- exact, alias, none, ambiguous
    position             TEXT,                      -- extractor's guess; used only to pick a stat schema
    data_type            TEXT NOT NULL,              -- 'actual', 'projection', or 'ranking' (Phase 4C prerequisite)
    scope                TEXT NOT NULL DEFAULT 'weekly',  -- 'weekly' or 'season' (season-long/preseason extension)
    ranking_type          TEXT NOT NULL DEFAULT '',   -- overall/QB/RB/WR/TE/FLEX when data_type='ranking'; '' otherwise.
                                                        -- A REAL structured field, not encoded into stat_name — and
                                                        -- deliberately '' rather than NULL for non-ranking rows, since
                                                        -- SQLite unique indexes treat NULL as never equal to itself,
                                                        -- which would silently break the dedup guarantee below for
                                                        -- every non-ranking row if this were left NULL.
    season_id            INTEGER NOT NULL REFERENCES seasons(season_id),
    week_id               INTEGER REFERENCES weeks(week_id),      -- NULL if extracted week didn't match a real week
    week_hint             INTEGER,                    -- raw extracted week number, for consistency-checking against week_id
    source_name           TEXT NOT NULL,              -- provenance source the user asserted at upload (NFL/Yahoo/ESPN/The Athletic/User)
    stat_name             TEXT NOT NULL,              -- literal 'ranking' for ranking rows (ranking_type is the real
                                                        -- structured field); the actual stat key otherwise
    stat_value            REAL,
    field_confidence      REAL NOT NULL,               -- 0.0-1.0, PER FIELD (not one score for the whole screenshot)
    validation_flags_json TEXT,                        -- plausibility/consistency notes, non-fatal
    disposition           TEXT NOT NULL DEFAULT 'pending',  -- pending, auto_accepted, sent_to_review, accepted, corrected_and_accepted, rejected
    resulting_record_id   INTEGER,                           -- once promoted: statistics.statistic_id (data_type='actual') or projections.projection_id (data_type='projection'). No FK — points into whichever table data_type indicates.
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposed_import ON proposed_observations(import_id);

-- Audit finding (Phase 4A -> 4B scheduler-readiness review): the application-level
-- "does an identical pending review item already exist?" check has a TOCTOU race —
-- two concurrent collector runs can both pass that SELECT before either commits,
-- producing two pending review items for the same observation. The app-level
-- check remains as a fast-path (avoids unnecessary validation work), but this
-- partial unique index is the actual guarantee: SQLite enforces it at the
-- UPDATE that flips disposition to 'sent_to_review', independent of timing.
-- Concurrent execution and full run-locking remain a Phase 4B scheduler
-- responsibility; this index only prevents the specific duplicate-review-item
-- failure mode, not general race conditions.
-- Phase 4C season-scope extension: week_id is nullable for scope='season' rows.
-- SQLite unique indexes never treat two NULLs as equal, so a bare week_id
-- column here would silently stop catching duplicates among season-scope
-- rows (the same failure mode ranking_type='' vs NULL avoided earlier).
-- COALESCE to a sentinel that can never be a real week_id (all real week_ids
-- are positive autoincrement integers).
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_pending_proposal
    ON proposed_observations(extracted_player_name, COALESCE(week_id, -1), stat_name, stat_value, ranking_type)
    WHERE disposition = 'sent_to_review';

-- Per-source, per-week weekly projection lock (2026-09-16 policy decision):
-- a source's weekly projections freeze the moment its FIRST successful
-- ingestion for that week completes -- not when the week/game begins.
-- Different sources upload on different days, so this can't live as a
-- single flag on the shared `weeks` row; it's tracked per source here.
-- See db.repository.ensure_weekly_projection_lock.
CREATE TABLE IF NOT EXISTS weekly_projection_locks (
    source_name     TEXT NOT NULL,
    week_id         INTEGER NOT NULL REFERENCES weeks(week_id),
    locked_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_name, week_id)
);

-- ============================================================
-- PHASE 4B: SCHEDULER
--
-- A scheduled_jobs row is orchestration metadata ONLY (what to collect, how
-- often, retry policy, last-known status). It never itself becomes trusted
-- data and holds no write path into statistics/projections — every actual
-- run still goes through ingest_collector_batch -> _route_field exactly
-- like a manually-triggered collection, per the frozen collector contract.
-- ============================================================

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name             TEXT NOT NULL,
    adapter_type            TEXT NOT NULL,                      -- key into ingestion.scheduler.ADAPTER_REGISTRY
    adapter_config_json     TEXT NOT NULL,                       -- kwargs to reconstruct the adapter
    season_id               INTEGER NOT NULL REFERENCES seasons(season_id),
    week_number             INTEGER NOT NULL,
    interval_minutes        INTEGER NOT NULL DEFAULT 60,
    max_retries             INTEGER NOT NULL DEFAULT 3,
    retry_backoff_minutes   INTEGER NOT NULL DEFAULT 15,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    next_run_at             TEXT,                                 -- when this job becomes due; NULL = due immediately
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    needs_attention         INTEGER NOT NULL DEFAULT 0,           -- set once retries are exhausted or a permanent/config failure occurs
    last_run_id             INTEGER REFERENCES collection_runs(run_id),
    last_run_status         TEXT,
    last_error              TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due ON scheduled_jobs(enabled, next_run_at);
