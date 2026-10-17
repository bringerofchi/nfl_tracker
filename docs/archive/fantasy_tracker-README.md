# NFL Fantasy Data Tracker

A local Flask and SQLite application for preserving source-specific NFL fantasy projections, rankings, and statistics across the 2026 season. It records provenance, resolves player identities without guessing, preserves versions and conflicts, and derives PPR fantasy points from trusted raw data.

## Current state

The tracker includes a dashboard, player views, manual entry, season and weekly projection support, rankings, review queue, ingestion screen, scheduler screen, CSV exports, a SQLite database, source adapters, and a test suite. The supplied project archive is the complete working tree; its Git history currently includes a Week 2 ESPN pull and a modified local `db/tracker.db`.

## Architecture

Each source-specific adapter acquires and normalizes observations only. All source data then follows one common path:

`source acquisition → NormalizedObservation → shared validation and identity resolution → provenance-aware storage → review queue or trusted projection/ranking/statistic`

This keeps source quirks out of the trust, persistence, versioning, and conflict-resolution rules.

## Implemented capabilities

- **Source-aware storage:** players, aliases, teams, seasons/weeks, games, statistics, projections, rankings, sources, imports, collection runs, raw and proposed observations, review items, and correction history.
- **Identity and review:** exact and alias resolution, no automatic player creation for unresolved input, ambiguity/conflict routing to the review queue, filtering, pagination, and scoped bulk rejection.
- **Versioned data:** identical re-imports are no-ops; changed source observations preserve history; source names remain distinct so one publisher cannot silently overwrite another.
- **PPR analysis:** QB/RB/WR/TE scoring, current trusted projections and actuals, player-level comparison and trend views, dashboard summary, and fantasy-point export.
- **Ingestion:** manual entry; screenshot extraction through a pluggable extractor; local normalized-file ingestion; The Athletic season XLSX ingestion; ESPN season PDF ingestion; and ESPN weekly live ingestion support.
- **Scheduling:** due-job selection, run locking, stale-run recovery, retry classification, idempotent reruns, and operator controls on `/scheduler`. An external scheduler still decides when to invoke due jobs.

## Sources and scope

The Athletic season-long XLSX adapter and ESPN season-projections PDF adapter are implemented. ESPN weekly ingestion modules and Week 1/Week 2 runner scripts are present. Yahoo, ESPN editorial rankings, and The Athletic live weekly rankings remain unvalidated acquisition paths: do not represent them as implemented feeds.

Season-scope data can be locked at the configured season start date so preseason baselines remain immutable for later comparisons. Weekly data remains independently versioned and source-specific.

## Running locally

From the project root, install the required local packages and start the Flask app:

```bash
pip install flask openpyxl
python app.py
```

Then open `http://localhost:5050`. Screenshot extraction additionally requires the optional Anthropic package and a locally supplied API key. Keep keys outside version control.

## Data and exports

The local database is `db/tracker.db`; treat it as the operational evidence store. The app provides tabular exports and a derived fantasy-points export. Before sharing a database or archive, remove private uploads, API credentials, subscriber-source material, and any data you do not have permission to redistribute.

## Verification

The archive includes an extensive test suite under `tests/`, including adapter, scheduler, review-queue, scoring, multi-source, projection-locking, season-scope, and regression coverage. Run it in an environment with Python and the required packages installed:

```bash
python -m pytest -q
```

## Limitations

- The app is local-first; it is not a hosted multi-user service.
- The scheduler logic is implemented, but external task scheduling is still required for unattended collection.
- No source adapter should be trusted beyond the real specimen and acquisition path it was tested against.
- Source comparisons remain source-specific; the app does not manufacture a consensus projection.

## Research/AAR value

The system’s high-value extract is a source-level record of preseason and weekly projections, actual statistics, rankings, observation provenance, identity-review outcomes, conflicts, corrections, and collection-run health. Keep these distinguishable by source and version rather than merging them into an invented consensus.
