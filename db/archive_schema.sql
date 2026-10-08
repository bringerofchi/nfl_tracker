-- Archive database schema (db/archive.db).
--
-- Mirrors raw_observations / proposed_observations / review_items from
-- db/schema.sql column-for-column. Primary keys are reused VERBATIM from the
-- live db -- an archived raw_id/proposed_id/review_id means exactly the same
-- thing it meant in tracker.db. This is intentional: it's what lets
-- provenance links (proposed_observations.resulting_record_id,
-- review_items.entity_id, etc.) keep meaning after a row moves here, and
-- lets a one-off debugging script ATTACH this file and join by the same ids
-- it already knows.
--
-- No FOREIGN KEY constraints against tracker.db tables (players, imports,
-- seasons, weeks, sources) -- SQLite cannot enforce FKs across separate
-- database files even under ATTACH. That's an accepted trade-off here
-- specifically because this file is cold storage, queried ad hoc, never
-- part of the live app request path -- unlike the weekly-split design this
-- project rejected, where the same limitation would have hit tables in the
-- app's hot path.

CREATE TABLE IF NOT EXISTS raw_observations (
    raw_id              INTEGER PRIMARY KEY,
    import_id           INTEGER,
    collection_run_id    INTEGER,
    content_type        TEXT NOT NULL,
    raw_content         TEXT,
    file_path           TEXT,
    created_at          TEXT
);

CREATE TABLE IF NOT EXISTS proposed_observations (
    proposed_id           INTEGER PRIMARY KEY,
    import_id             INTEGER NOT NULL,
    raw_id                 INTEGER,
    extracted_player_name TEXT NOT NULL,
    resolved_player_id     INTEGER,
    identity_match_type    TEXT,
    position               TEXT,
    data_type              TEXT NOT NULL,
    scope                  TEXT NOT NULL DEFAULT 'weekly',
    ranking_type           TEXT NOT NULL DEFAULT '',
    season_id              INTEGER NOT NULL,
    week_id                 INTEGER,
    week_hint               INTEGER,
    source_name             TEXT NOT NULL,
    stat_name               TEXT NOT NULL,
    stat_value              REAL,
    field_confidence        REAL NOT NULL,
    validation_flags_json    TEXT,
    disposition              TEXT NOT NULL DEFAULT 'pending',
    resulting_record_id      INTEGER,
    created_at                TEXT,
    resolved_at                TEXT
);

CREATE TABLE IF NOT EXISTS review_items (
    review_id       INTEGER PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    entity_id       INTEGER,
    reason          TEXT NOT NULL,
    confidence      TEXT,
    source_name     TEXT,
    details_json    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT,
    resolved_at     TEXT,
    resolved_by     TEXT
);

-- Audit trail of the archival process itself: every --apply run writes one
-- row per table it touched. This is what a rollback reads to know exactly
-- which rows to copy back and which manifest row to remove.
CREATE TABLE IF NOT EXISTS archive_manifest (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_started_at      TEXT NOT NULL,
    run_finished_at     TEXT,
    table_name          TEXT NOT NULL,
    rows_moved          INTEGER NOT NULL DEFAULT 0,
    bytes_reclaimed_est INTEGER,
    min_id              INTEGER,
    max_id              INTEGER,
    week_ids_json       TEXT,
    dry_run             INTEGER NOT NULL DEFAULT 1,
    notes               TEXT
);
