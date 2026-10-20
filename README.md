# NFL Fantasy Data Tracker — Phase 1 & 2

## What's built so far
- **Data foundation** (schema): players with identity resolution/alias matching,
  teams, seasons/weeks, games, statistics, projections, rankings, sources,
  collection runs, imports, raw observations, review queue, corrections/audit trail.
- **Manual entry UI**: add players, enter projections (PPR, per-position stat
  fields you selected), enter actual statistics, enter rankings, dashboard with
  per-source completeness, review queue for conflicts/ambiguous matches, CSV export.

## Decisions this build reflects
- Scoring: PPR · Season: 2026 only · Trigger: manual now, built to support scheduling later
- Screenshot extraction: high-confidence auto-accept / uncertain -> review
  (not built at the time of this section; subsequently implemented in Phase 3 — see below)
- Deployment: local app (Flask + SQLite, no external services)
- Stat fields:
  - QB: pass yards, pass TDs, INTs, rush yards, rush TDs, completions/attempts
  - RB: rush yards, rush TDs, receptions, receiving yards, receiving TDs, fumbles lost
  - WR: receptions, receiving yards, receiving TDs, rush yards, rush TDs, fumbles lost
  - TE: receptions, receiving yards, receiving TDs, fumbles lost

## Run it
```
cd nfl_tracker
pip install flask
python3 app.py
```
Then open http://localhost:5050

## Tested
- Player identity resolution (dedupes name variants like "Ja'Marr Chase" vs "Jamarr Chase")
- Ambiguous-match detection routes to Review Queue instead of guessing
- Projection entry with versioning (history preserved, not overwritten)
- Statistic entry with conflict detection: re-entering a different value for the
  same player/week/stat/source preserves the original, adds a new version, and
  flags it in the Review Queue rather than silently overwriting
- Ranking entry, CSV export, dashboard completeness widget

**Running the main suite, `tests/`** (current as of 2026-10-17; the phase
notes below quote older, smaller counts):

```
python3 -m unittest discover -s tests
```

This runs 232 tests. Six tests in `tests/test_season_projection_locking.py`
fail on the current code; the cause has not been diagnosed. Treat those six as
the known baseline, so a change is clean when it adds no failures beyond them.
Use `unittest` rather than `pytest`: `espn_adapter/live_test.py` makes an
unguarded live network call at import time and matches pytest's default
collection pattern, and `unittest discover -s tests` never collects it.

The separately located `espn_adapter/` package has its own test file,
`espn_adapter/test_espn_adapter.py`, which is not part of the command above and
is not counted in the 232. It is written for pytest and is run on its own:
`python3 -m pytest -q espn_adapter/test_espn_adapter.py` (19 tests passed when
checked on 2026-10-17). Do not run bare `pytest` at the repo root, since that
also collects `espn_adapter/live_test.py`.

## Phase 4B: scheduler (this update)
Orchestration around the already-proven `ingest_collector_batch()` — nothing
about the data/ingestion architecture changed, per your framing.

- **`ingestion/scheduler.py`** — `Scheduler` class, timer-independent (no
  thread, no cron dependency, no sleep loop). Something external decides
  *when* to call `run_due_jobs(now=...)`; the scheduler only decides *what*
  is due and reacts to outcomes. `now` is always injectable for deterministic
  testing.
- **Run locking**: a partial unique index on `collection_runs(source_id,
  season_id, week_id) WHERE status='started'` — the same DB-level pattern as
  the 4A audit's duplicate-review guard, not an application-level check with
  a race window. `ingest_collector_batch` converts the resulting
  `IntegrityError` into a graceful `"run_locked"` outcome. **A real bug was
  caught and fixed while testing this**: the failed `INSERT` left an implicit
  transaction open, which meant the connection that *lost* the lock race kept
  holding a database-level lock afterward, capable of blocking the very run
  it had just deferred to — a rollback was missing. Found via the same
  deterministic-interleaving test technique used in the 4A audit (real-thread
  timing under the GIL proved too unreliable for a critical section this
  short to test meaningfully — documented in `tests/test_scheduler.py`).
- **Stale-run detection/recovery**: `Scheduler.recover_stale_runs()` uses
  the `find_stale_collection_runs()` helper added during the 4A audit,
  marking runs stuck at `'started'` past a threshold as `'stale'` — which
  also frees the run-lock, since the lock only holds while `status='started'`.
- **Retry policy**: `AdapterUnavailable` now carries a `transient: bool`
  flag. `LocalFileSourceAdapter` sets it correctly — a missing file
  (`transient=True`, could be a timing issue, worth a short-backoff retry)
  vs. malformed JSON content (`transient=False`, retrying immediately
  reproduces the identical failure). `collection_runs.failure_type` records
  which. The scheduler classifies every outcome into `success` /
  `transient_failure` / `permanent_failure` / `config_error` / `locked` and
  applies short backoff only to genuinely transient failures — permanent
  failures (including a batch where every observation was malformed) fall
  back to the job's normal interval and flag `needs_attention` immediately
  rather than spamming retries against bad data.
- **Safe restart/idempotency**: exercised through the scheduler's own retry
  path (fail → source recovers → retry succeeds → later identical re-run
  stays a no-op), building on the idempotency guarantees already proven in
  the 4A audit.
- **A related pipeline refinement**: `ingest_collector_batch` no longer
  re-raises on an unexpected mid-batch exception (the 4A audit's fix marked
  `collection_runs` as `'error'` and re-raised) — it now returns a graceful
  result instead. An orchestrator managing many jobs needs every call to
  come back as an actionable result, not sometimes throw; nothing is hidden,
  the failure is still fully visible via `collection_runs.status='error'`
  and the returned `error` field.
- **Status/visibility**: `Scheduler.job_status()` / `list_status()` — per
  job: enabled/disabled, currently running, next run time, consecutive
  failures, needs-attention flag, last run's status/error. New `/scheduler`
  page: job list with run-now/enable/disable, "run all due jobs",
  "recover stale runs", and a form to add new jobs.
- 15 new tests (`tests/test_scheduler.py`) covering exactly what was asked:
  due-job selection, successful runs and rescheduling, transient vs.
  permanent vs. config-error retry policy (including exhausting the retry
  budget), concurrent invocation (proven deterministically, not via flaky
  thread timing), stale-run recovery unblocking the lock, and safe
  reruns/idempotency after an interrupted run.

**Not built** (Phase 4C, explicitly next per your sequencing): real source
adapters (Yahoo/ESPN/The Athletic) behind the same frozen `SourceAdapter`
interface — `LocalFileSourceAdapter` remains the only one proven out, on
purpose.

## Architectural principle: adapter responsibility boundary
Formalized after the Phase 4C source-acquisition research, worth stating
explicitly since it's what keeps source-specific uncertainty contained:

**An adapter is responsible only for acquisition and normalization. It must
never contain source-specific trust or persistence logic.**

```
Yahoo / ESPN / The Athletic
       ↓
source-specific acquisition   (the ONLY part that's source-specific)
       ↓
NormalizedObservation
       ↓
shared pipeline (_route_field)
       ↓
validation / identity resolution / confidence / review
       ↓
trusted statistic / projection / ranking
```

Concretely: an adapter's `fetch()` may know about HTTP headers, OAuth
tokens, HTML structure, or a source's own player IDs. It must never decide
whether an observation is trustworthy, never touch `statistics`/
`projections`/`rankings` directly, and never bypass identity resolution or
validation. Every adapter — `LocalFileSourceAdapter` today, a real Yahoo/
ESPN/Athletic adapter later — is judged only on whether it correctly
produces `NormalizedObservation`s from whatever it acquired. That's what
makes "the adapter itself should be relatively small" true: all the hard
parts (trust, persistence, conflict handling, provenance, scheduling)
already exist and don't need to be reasoned about per-source.

## Phase 4C — net status
- **Frozen and proven**: core schema/identity resolution, manual entry,
  versioning/conflict resolution, screenshot ingestion, the shared
  ingestion contract, `SourceAdapter` interface, raw-observation/provenance
  chain, the ranking ingestion extension, scheduler + run-locking +
  stale-run recovery + retry classification + restart/idempotency + review-
  queue safeguards, `LocalFileSourceAdapter` as the end-to-end reference.
- **Empirically unresolved, deliberately not worked around**: Yahoo, ESPN
  editorial rankings, and The Athletic weekly rankings acquisition. The
  ESPN general-API capture is real evidence of *a* reachable structured
  surface — explicitly not evidence that ESPN's *ranking* content is
  acquirable through it. Those are kept as separate claims in this record
  on purpose.
- **Phase 4C prerequisite: complete. Phase 4C source adapters: not started.**
  The next real development step requires at least one genuine captured
  ranking specimen (browser network-tab capture, or a saved authenticated
  page) for one source — not before.

## Full roster built from real preseason projections (this update)
"Track every player in the preseason projections" is an explicit, unambiguous
instruction — not a guess — so this is now done for real, against the live
database, not a demo.

- **`scripts/bulk_create_players_from_review_queue.py`** — new, reusable.
  Finds every pending `player_not_found` review item, creates the player,
  then explicitly accepts that player's observations against the newly
  resolved identity — reusing `accept_proposed_observation`, not a separate
  write path. Deliberately scoped: only touches `player_not_found` items
  with a known position; `ambiguous_identity` items are left untouched on
  purpose, since "which of these two already-existing players did you mean"
  isn't something a blanket "create everyone" instruction can resolve.
- **Run for real**: 485 new players created, 21 name-variant duplicates
  between The Athletic and ESPN's own spelling of the same real player
  correctly caught by identity resolution and merged into one player rather
  than creating two. All 3,774 pending observations accepted, 0 failures,
  Review Queue now empty.
- **Verified with real data**, not synthetic: Sam Darnold's season
  comparison page now genuinely shows both sources' real, different
  projections side by side (4012 vs 4135 pass yards, 24 vs 26.52 pass TDs,
  etc.) — the actual payoff of everything built this session.

**195 tests still passing.** The delivered database now has a real,
complete roster of every player from both preseason projection sources,
season projections locked to trigger on Sept 9, 2026, and an empty Review
Queue.

## Review Queue scale fix (this update)
The real problem from last session, fixed and validated against your actual
3,774-item queue, not synthetic data.

- **`review_items.source_name`** — new column, denormalized from
  `details_json` for SQL-level filtering (scanning JSON text per row isn't
  viable at this scale). Migrated the live database in place: added the
  column, backfilled all 3,774 existing rows from their JSON — confirmed
  the split matches exactly (1,265 ESPN / 2,509 Athletic, the same numbers
  from the imports themselves).
- **Pagination** — 25 items per page. The unfiltered queue page went from a
  **5MB response down to ~35KB**, confirmed against your real data, not a
  synthetic test.
- **Filtering** — by source, reason, and entity type, with live counts shown
  in each dropdown (e.g. "ESPN (1265)") so you know what's actually there
  before filtering.
- **Bulk-reject, deliberately not bulk-accept.** You can clear a whole
  filtered slice in one action (tested for real: rejected all 1,265 ESPN
  `player_not_found` items in one click, verified only those were removed,
  everything else untouched). Bulk-*accept* was deliberately never built —
  approving hundreds of unresolved identities or unverified values sight
  unseen would defeat the entire purpose of the Review Queue. The route
  also refuses to run with no filter at all, so it can't be used to blindly
  clear the whole queue by accident.
- 9 new tests (`tests/test_review_queue_scale.py`): source_name populated on
  creation (for all three creation paths — proposed observations, statistic
  conflicts, ranking conflicts), SQL-level filtering, pagination math, and
  the bulk-reject safety guard and scoping, all through real HTTP requests.

**195 tests total, all passing.** The delivered database keeps your real
imported state exactly as it was — 3,774 pending items, season start date
set — just with the new column populated and the page now actually usable.

## Working session: items 1-4 (this update)
Picked up where the last session left off — season start date, real imports,
roster, and the comparison view.

1. **Season start date** — set to **2026-09-09**, the NFL's actual confirmed
   Wednesday-after-Labor-Day opener (corroborated across Yahoo Sports, Fox
   Sports, NFL.com, NBC Sports, Wikipedia — looked up, not guessed). Season
   projections will lock automatically on that date.
2. **Real imports run against your actual files** — not test fixtures.
   `2026-FFB-Projections-0819a.xlsx` (The Athletic, full file) and 18 of the
   21 real ESPN PDF pages you provided (missing pages 1, 4, 7, 10 — not
   uploaded). Results: **2,509 Athletic + 1,265 ESPN observations, all
   correctly routed to the Review Queue** (0 auto-accepted, since the roster
   was empty going in) — proving `identity resolution never guesses` held
   under real full-scale data, not just small tests. The duplicate guard
   also proved itself for real: 345 duplicate observations from overlapping
   ESPN pages were correctly caught and skipped rather than double-counted.
3. **Roster — genuinely not something I did, and said so rather than fake it.**
   I deliberately did not invent a roster. Which specific players you want
   tracked is a judgment call only you can make — picking for you would be
   exactly the kind of guessing this whole architecture exists to prevent.
   **A real problem surfaced while testing this**: the Review Queue at
   3,774 pending items renders a 5MB page — technically functional, not
   practically usable by clicking through one card at a time. This is a
   genuine UX gap the review-queue design didn't anticipate at this scale.
4. **Season projection comparison view** — new page,
   `/players/<id>/season`, showing every source's current season-long
   projection side by side (stat by stat, plus each source's independently-
   computed PPR total), never merged. Verified against real data in a
   throwaway DB copy (not the delivered one): The Athletic's actual Josh
   Allen projection rendered correctly — 4001.64 pass yards, 371.04 total
   points. 6 new tests (`tests/test_season_projection_comparison.py`),
   including one proving a stat missing from one source shows as blank, not
   a fabricated zero, and totals are computed independently per source.

**186 tests total, all passing.** The delivered database intentionally
keeps the real imported state (season start date, 3,774 pending review
items) rather than being reset clean — that state *is* the real progress
from this session, not test data to discard.

## Real bug found and fixed: add_projection was never idempotent (this update)
Surfaced by re-uploading a page that turned out to be a byte-identical
duplicate of one already imported (page 8, confirmed via md5 hash match).

- **What happened**: re-importing the exact same real ESPN page created 116
  spurious "version 2" rows — same value as version 1, no real correction
  behind them, pure audit-trail noise. Root cause: unlike `add_statistic`
  and `add_ranking` (both already idempotent no-ops on an identical
  re-entry), `add_projection` had never had that check — a gap flagged in
  passing much earlier this session but not fixed until it caused a real,
  observable problem.
- **Fixed**: `add_projection` now returns `(id, was_new)`, matching its
  siblings, and no-ops when the existing current value is identical. All 3
  call sites updated to unpack the tuple.
- **A related ergonomics decision**: if a *locked* season projection gets
  re-submitted with the identical value (e.g. a harmless re-import after
  the season already locked), that's now a silent no-op, not an error —
  nothing is actually being modified. Only a genuine attempted *change* to
  frozen data still raises. New test covers this specifically.
- **Verified against real data**: re-ran the same real duplicate page a
  third time — total projection row count stayed flat (3592 → 3592, was
  3592 → 3592+116 before the fix) while the pipeline still correctly
  processed all 128 observations.
- 2 new/rewritten tests in `tests/test_season_scope_extension.py`
  (idempotent no-op + differing-value still versions correctly), 1 new test
  in `tests/test_season_projection_locking.py`. **197 tests total, all
  passing.**

The 116 spurious versions created before this fix (during earlier testing
this session) were left in place rather than deleted — they're an accurate
record of what actually happened, and this project never deletes historical
data even when it turns out to be redundant. They're simply superseded,
harmless, and won't recur going forward.

## Multi-source projections + ESPN adapter (this update)
Building toward "compare projected vs actual stats, per site, weekly."

**A critical bug found and fixed first**: `add_projection`'s identity/
uniqueness key never included `source_name` — unlike `add_statistic` and
`add_ranking`, which were correctly source-scoped from the start (sec. 12).
This meant a second source's independent projection for the same player/
stat/week was silently treated as a *correction* to the first source's
value rather than a separate observation — reproduced directly: entering
The Athletic's projection then ESPN's for the same player caused The
Athletic's row to flip to `is_current=0`, as if ESPN had "corrected" it.
That would have completely defeated per-site comparison, the whole point of
this work. Fixed by adding `source_name` to the identity lookup (via
`source_name IS ?`, same NULL-safety reasoning as the `week_id`/
`ranking_type` fixes before it). 6 new tests
(`tests/test_multi_source_projections.py`) proving 2 and 3 sources coexist,
for both weekly and season scope, without disturbing same-source
versioning.

**A related gap in the fantasy service layer, also fixed**: `compute_projected_fp`
and `get_projection_stat_values` had never been extended to query
`scope='season'` data at all — `week_id=None` would never match via the old
bare `week_id=?` equality. Fixed the same way (`week_id IS ?`) and added a
`scope` parameter throughout.

**`ingestion/espn_projections_adapter.py`** — the second real adapter,
parsing ESPN's "Complete 2026 Projections" page saved as PDF. Built and
tested against a genuine trimmed excerpt of your actual uploaded file
(`tests/fixtures/espn_projections_excerpt.pdf` — two real physical pages,
not fabricated). Column-position parsing (not exact-offset), position-aware
stat mapping, C/A completions/attempts splitting, `--` correctly treated as
missing. **A real, honest limitation, not glossed over**: ESPN's print
layout consistently truncates the last stat column for every position (QB
rushing TDs, RB/WR/TE's trailing TD column) — those are simply never
produced, not guessed or backfilled. Deliberately imports only "2026
PROJECTIONS," not ESPN's own "2025 STATISTICS" column (that's last
season's historical box score, not what "compare 2026 projections vs 2026
actuals" needs — real 2026 actuals come from the NFL canonical source at
weekly granularity). 13 new tests, including one proving The Athletic's and
ESPN's real, genuinely different projections for the same real player
(Jacoby Brissett) now correctly coexist and produce independently
computable fantasy-point totals.

**Confirmed decision from this session**: four consistent real ESPN
specimens were enough to build against — didn't wait for all 21 pages, and
the adapter is designed to run against any of them (single PDF or a
directory of PDFs in one `fetch()` call) whenever you're ready to run it
for real via `scripts/import_espn_season_projections.py`.

**Still open, matching your stated multi-week goal**: weekly (not just
season-long) per-source projections and rankings are already fully
supported by the storage/pipeline layer (proven by the multi-source tests
above, plus the existing ranking-extension work) — but no *adapter* for
weekly per-site data has been built yet, since no real weekly specimen has
been captured. That's the natural next step once the season is underway
and such a specimen exists.

## Season-long projection locking
A real requirement, not a hypothetical: season-long projections must freeze
on the season's actual calendar start date, so they remain a stable baseline
for comparing against actual full-season stats later.

- **Found while building this**: the pre-existing `is_frozen` column on
  `projections` (meant for "the week has begun") was never actually enforced
  anywhere — nothing set it, nothing checked it. It was inert. This update
  makes the underlying mechanism real, not just adds a new flag.
- `seasons.start_date` — a real calendar date, never guessed or defaulted.
  Set explicitly via `repo.set_season_start_date()` or the dashboard form.
- `repo.ensure_season_lock()` — idempotent check-and-lock. No-op until
  `start_date` is set and reached; freezes every currently-current
  `scope='season'` projection exactly once. Called automatically both as a
  side effect of `add_projection` (so a write arriving on/after the start
  date correctly locks whatever was current *before* evaluating the
  incoming write) and passively on dashboard load — no cron/scheduler
  dependency needed.
- **Locking is enforced, not advisory**: `add_projection` now raises
  `ValueError` if the existing current row for an identity is frozen,
  instead of silently versioning over it. The frozen baseline is
  guaranteed to stay exactly as it was at lock time — that's what makes
  "compare against actuals at season's end" meaningful.
- Scoped narrowly to `projections` only, per what was actually asked —
  season-long rankings remain freely editable; locking those too would be
  a small, separate follow-up if wanted.
- 12 new tests (`tests/test_season_projection_locking.py`): no-start-date
  never locks, before/on/after start date, idempotency, the write-triggers-
  lock-automatically path (using an injectable `today` parameter, same
  pattern as the scheduler's `now`), locked values genuinely immutable,
  weekly projections and rankings unaffected, and a full pipeline import
  after lock routing to an error gracefully rather than crashing the batch.
- Dashboard shows lock status and a simple form to set the start date if unset.

## Phase 4C first real adapter: The Athletic season-long projections
The first actual break from research-status, using a genuine specimen you
provided (a subscriber-distributed xlsx projections/rankings model,
associated with The Athletic).

**Important distinction from what was frozen before**: this specimen turned
out to be a **season-long/preseason draft-prep model**, not a weekly
rankings feed — no per-week breakdown anywhere in the workbook, just a
single season-total projection and a set of preseason rank orderings per
player. That's structurally different from what the schema modeled (which
assumed every projection/ranking has a specific week), so before writing
any adapter code, the schema itself needed a small, additive extension:

- **`scope` column** added to `projections` and `rankings` (`'weekly'` or
  `'season'`), `week_id` made nullable. Same principle as the earlier
  `ranking_type` extension: a real structured field, never inferred from
  `week_id` being null.
- **Two real bugs found and fixed while building this**, both the same
  class of issue already caught once before:
  1. `week_id=?` comparisons are never true against `NULL` in SQL — silently
     breaks idempotency/conflict-detection for every season-scope row.
     Fixed to `week_id IS ?` everywhere it mattered, including the
     pending-duplicate unique index (`COALESCE(week_id, -1)`, since a plain
     unique index never treats two `NULL`s as equal either).
  2. `plausibility_flags` was applying single-game thresholds (e.g. 550 max
     pass yards) to season-long totals, which would've flagged nearly every
     real season projection (4000+ pass yards is completely normal for a
     season) as implausible. Fixed by skipping that check entirely for
     `scope='season'` rather than inventing unproven season-scale bounds.
- 23 new lifecycle tests (`tests/test_season_scope_extension.py`), full
  149-test suite green.

**`ingestion/athletic_xlsx_adapter.py`** — the actual source-specific
acquisition/normalization code, nothing more. Reads the workbook's QB/RB/WR/TE
sheets for raw season-total stat projections and the Rankings sheet for
positional ranks; explicitly does NOT import the workbook's own precomputed
FPS/AUC$/PPR columns (raw stats only — this app always derives fantasy
points itself); explicitly does NOT parse the sheet's duplicate flex-oriented
ranking groups or the separate overall/VORP sheet (out of scope until
there's a clear reason to). Every observation it produces still goes through
the unmodified shared pipeline — this file has no trust or persistence logic
of its own.

**Run against the real file**: 24 fields auto-accepted for 4 known test
players with values matching the source exactly, 2,485 observations
correctly routed to the Review Queue for every other player in the file who
wasn't already in a roster — none auto-created. `tests/test_athletic_xlsx_adapter.py`
(12 tests) runs against `tests/fixtures/athletic_2026_projections_excerpt.xlsx`
— a real, genuine excerpt (actual header rows and actual player rows copied
verbatim from your file), not a fabricated fixture. The full 1.3MB personal
file itself was deliberately NOT bundled into this project — kept only as
much real data as needed to test against, out of respect for it being your
subscription content.

**To use this yourself**: `python3 scripts/import_athletic_season_projections.py /path/to/your/file.xlsx`
— safe to re-run whenever you get an updated copy; unchanged values are
idempotent, changed values version and (for rankings) flag a conflict.
Registered in the scheduler's `ADAPTER_REGISTRY` as `AthleticSeasonXlsxAdapter`
too, so it can also run as a scheduled job like any other adapter.

**New dependency**: `openpyxl` (already used elsewhere in this project's
tooling; add `pip install openpyxl` alongside `flask` if not already present).

**Not built**: a UI page to view season-scope projections/rankings — they're
fully stored and queryable, but the dashboard/player-detail views are still
week-indexed and won't show them yet. Also unchanged: Yahoo, ESPN editorial
rankings, and The Athletic's *live web* rankings remain exactly where the
research left them — this is a different kind of specimen (a distributed
file, not a scraped page) and doesn't change that status.

## Phase 4C — source acquisition research (this update, no code changed)
No adapter code was written this pass. The prior "prerequisite" update froze
the generic ranking ingestion contract; this pass was research into whether
Yahoo, ESPN, and The Athletic actually have a real, reproducible acquisition
path for weekly rankings. **Net status: research-status, not adapter-ready,
for all three sources.**

- **ESPN general JSON API — empirically validated, kept separate.** A live,
  unauthenticated fetch of `site.api.espn.com/apis/site/v2/sports/football/
  nfl/scoreboard` returned a real captured payload: explicit `season.type`/
  `week.number` (maps cleanly to `season_id`/`week_number`), and numeric
  athlete `id`/`displayName`/`position.abbreviation` as a real identifier
  scheme. **This is deliberately NOT treated as evidence that ESPN's
  ranking acquisition path exists** — it's a stats/scoreboard endpoint, not
  a rankings endpoint. Useful groundwork for a future projection/stats
  surface; doesn't unblock a ranking adapter.
- **ESPN editorial rankings (the actual overall/QB/RB/WR/TE weekly rankings
  content)** — not validated. Direct fetch attempts against the article
  pages returned no extractable content. The ranking-adjacent hidden
  endpoint (`kona_player_info`/`leaguedefaults`) is described by third-party
  documentation but was never actually captured live in this session — a
  tool limitation (fetches are restricted to URLs that were themselves
  returned as a search result, not URLs merely quoted inside another page),
  not evidence the endpoint doesn't work.
- **Yahoo rankings** — not validated. An official OAuth-gated API exists,
  but no evidence was found that it exposes a discrete weekly ranking feed
  as opposed to per-player stats/projections; documentation was found, no
  live capture was possible without app registration + user credentials.
- **The Athletic rankings** — not validated, blocked on authentication.
  Search couldn't even surface the content (in sharp contrast to how
  heavily-indexed ESPN's editorial content is), consistent with — but not
  proof of — a harder paywall. No fixture was invented for this source.

**What unblocks the next step**: someone with browser access needs to open
each source's actual rankings page, check the Network tab for an XHR/fetch
call to a JSON endpoint (if found, that's the real capture) or confirm the
data is server-rendered HTML/requires an authenticated session — the two
cases that determine what an adapter for that source would even look like.
Short of that, no source-specific adapter should be built, since it would
be built against fabricated assumptions about payload shape.

**Not built, on purpose**: any Yahoo/ESPN/Athletic adapter code, any fixture
files for these three sources. `LocalFileSourceAdapter` remains the only
adapter proven end-to-end (Phase 4A), now also ranking-capable (Phase 4C
prerequisite).

## Phase 4C prerequisite — Ranking Ingestion Extension (this update)
Frozen separately from the original 4A contract, per your framing — proven
before any Yahoo/ESPN/Athletic adapter code gets written, not discovered mid-build.

**The gap this closes**: Yahoo/ESPN/The Athletic were seeded back in Phase 1
as `ranking_provider` sources, but the frozen collector contract only knew
`data_type` values `'actual'`/`'projection'`, with no path to `rankings` at
all. Building "adapters" for these three sources against the old contract
would have meant misusing it.

- `NormalizedObservation.data_type` now also accepts `'ranking'`, with a new
  `ranking_type` field (`overall`/`QB`/`RB`/`WR`/`TE`/`FLEX`) — a **real
  structured column** on `proposed_observations`, not encoded into
  `stat_name`, per your explicit call. Stored as `''` (never `NULL`) for
  non-ranking rows — `NULL` would have silently broken the pending-duplicate
  unique index, since SQLite never treats `NULL = NULL` in a unique
  constraint.
- `_route_field` — the single shared routing core for every ingestion
  entry point — now branches to `repo.add_ranking` on auto-accept for
  ranking observations, exactly the same way it already branches to
  `add_statistic`/`add_projection`. No parallel ranking pipeline.
- **Behavior change, requested explicitly**: `repo.add_ranking` previously
  silently superseded a differing value with no review. It now flags a
  conflict via the Review Queue, mirroring `add_statistic` exactly — new
  `repo.resolve_ranking_conflict()` (approve keeps the new value trusted,
  reject reverts to the prior value), wired into the `/review` route the
  same way statistic conflicts already were. Confirmed live through the
  manual `/rankings` entry route, not just the automated path.
- `rankings.provenance_ref` added for parity with statistics/projections.
- `accept_proposed_observation` extended with a ranking branch (accept,
  accept-with-correction, re-validates on correction).
- `LocalFileSourceAdapter`'s fixture JSON format extended with an optional
  `ranking_type` field for `data_type: "ranking"` entries.
- Confirmed via test: a WR-position rank of 5 and an overall rank of 5 for
  the same player/week are correctly treated as distinct observations, not
  falsely deduplicated by either the app-level check or the DB-level unique
  index — a real bug that a naive "just add ranking_type to the composite key"
  approach would have introduced if `NULL` had been used for non-ranking rows.
- **Explicitly deferred, per scoping decision**: screenshot-based ranking
  extraction (`ExtractedField`/the AI vision prompt) — this pass wires
  ranking support into the shared `_route_field`/repository layer and the
  collector path fully; extending `ingest_screenshot` to detect rankings
  from a screenshot is separate future work, not needed for the Yahoo/ESPN/
  Athletic adapters this unblocks.
- 21 new tests (`tests/test_ranking_extension.py`) proving the full
  lifecycle: validation (positive-integer-only), auto-accept, review
  routing with `ranking_type` visible, accept/accept-with-correction/reject,
  identity resolution (never auto-creates a player), versioning/idempotency,
  conflict flagging and resolution (including one test through the actual
  Flask `/review` route, not just the repository layer), and full
  provenance. Ran the entire existing 93-test suite alongside — zero
  regressions, 114 total.

**Not built**: Yahoo/ESPN/The Athletic adapters themselves — this was
explicitly the prerequisite pass before touching them.

## Phase 4A → 4B scheduler-readiness audit
Requested before any scheduler gets built. Two real gaps were found and fixed; the rest were verified sound.

**Found and fixed:**
1. **Transaction boundaries / crash recovery.** `repo.add_statistic`/`add_projection`
   commit internally, so a collector batch was never atomic — reproduced a mid-loop
   exception and confirmed `collection_runs` got stuck at `status='started'` forever,
   even though one observation had already committed. A scheduler doing run-locking
   or safe-restart-after-interruption would have no way to tell that run apart from
   one still genuinely in progress. **Fixed**: the observation-processing loop is now
   wrapped so any unexpected exception always drives the run to a terminal `'error'`
   status before re-raising. Added `find_stale_collection_runs()` — a read-only
   helper a scheduler can poll for runs stuck at `'started'` past a threshold, for
   the specific case a Python exception *can't* catch: the process being killed
   outright. That last case is honestly out of this pipeline's reach; it's stated
   as a Phase 4B scheduler responsibility, not silently declared "fixed."
2. **Concurrent execution race.** Reproduced, with two connections executing the
   exact interleaving by hand, that the app-level "does a pending duplicate already
   exist?" check has a TOCTOU window: two concurrent runs can both pass that SELECT
   before either commits. **Fixed** with a DB-level partial unique index
   (`idx_unique_pending_proposal`) that SQLite enforces regardless of timing —
   confirmed directly that a second identical transition now raises
   `IntegrityError`, and that `_route_field` catches it and resolves to a safe,
   non-duplicate outcome instead of crashing the batch. General run-locking (two
   full collector runs executing simultaneously) remains explicitly a Phase 4B
   scheduler responsibility — this fix only closes the specific duplicate-review-item
   failure mode.

**Also fixed while auditing "source/version metadata sufficiency":** projections
had no structured provenance field (only free-text `notes`), unlike statistics —
added `projections.provenance_ref` for parity, so both tables answer "where did
this come from" the same way.

**Verified sound, no change needed:** idempotency (duplicate collection was
already a no-op via `repo.add_statistic`'s existing identical-value check),
duplicate-pending-review dedup at the application level (now backed by the DB
constraint above), failure/retry safety (a fetch failure writes nothing, so
retrying is safe), and the full provenance chain (source → collection run →
raw payload → proposed observation → trusted statistic → fantasy points, all
independently queryable — see `tests/test_source_adapter.py::test_full_provenance_chain_is_queryable`).

10 new tests (`tests/test_scheduler_readiness_audit.py`), all passing, each
reproducing the failure mode first and then proving the fix.

**Not built at the time of this audit**: Phase 4B itself (scheduling, run-locking, retry policy).
This was explicitly an audit pass, not a feature addition — Phase 4B was
subsequently implemented; see the Phase 4B section above.

## Phase 4A: source adapter framework
Freezes the collector contract before any scraper gets written, per your review.

- **`completions_attempts` split**: decided explicitly rather than deferred —
  split into `pass_completions` and `pass_attempts` (schema, stat definitions,
  validation, extractor prompt, all updated). A new cross-field check,
  `ingestion/validation.py::completions_attempts_flags()`, flags (doesn't
  block) completions > attempts for review, since that check needs both
  values at once and can't live in the existing per-field validator.
- **`ingestion/collector.py`** — the frozen contract:
  - `NormalizedObservation` — the one shape every collector must produce
    (player name, position hint, week, data_type, stat_name, value,
    per-observation confidence, optional team hint). Nothing past this
    dataclass knows or cares where the data came from.
  - `SourceAdapter` — interface with one method, `fetch(season_id, week_number)`.
  - `LocalFileSourceAdapter` — the one source implemented completely for
    Phase 4A. Reads a normalized JSON payload from disk. **Why file-based,
    not live HTTP:** this sandbox's network egress allowlist doesn't include
    any sports data provider, so a live scraper literally cannot be exercised
    here. A file-based adapter is a real, honest collection mechanism (many
    tools ingest weekly data exports this way) and proves the full contract
    end-to-end. An HTTP-based adapter would implement the exact same
    `SourceAdapter` interface — only `fetch()`'s internals differ (a network
    call instead of a file read); no pipeline change would be needed.
  - `AdapterUnavailable` — raised on source outage/malformed payload; the
    pipeline fails the run cleanly rather than writing partial data or crashing.
- **`ingestion/pipeline.py::ingest_collector_batch()`** — reuses the exact
  same `_route_field()` core that `ingest_screenshot()` uses (refactored out
  of Phase 3's code, not reimplemented), so screenshot extraction and
  collector batches are provably the same ingestion contract, not two
  similar-but-diverging ones. Writes a `collection_runs` row (started/
  completed/status/collector_version) and an immutable raw JSON payload into
  `raw_observations`, tagged with `collection_run_id` for full provenance.
  Never creates a player on its own — unresolved/ambiguous identity always
  routes to the Review Queue, same rule as Phase 3.
- Duplicate-pending-review guard: a scheduled re-run before a human reviews
  the previous run's flagged item won't spam a second identical review item.
  This is new in the collector path (screenshots don't need it — a re-upload
  is a deliberate one-off action, not a recurring background job).
- 15 new tests (`tests/test_source_adapter.py`) covering exactly what was
  asked: duplicate collection, changed source data (both pre-trust and
  post-trust), missing players, ambiguous identity, malformed observations
  (individually and as a whole batch), source outage (missing file and
  invalid JSON), unknown source names, and a full provenance-chain query
  (source -> collection run -> raw payload -> proposed observation ->
  trusted statistic -> fantasy points).

**Not built at the time of this section** (explicitly out of scope per your
Phase 4A framing): Phase 4B (scheduling — `collection_runs` now has
everywhere a scheduler would hook in, but nothing schedules yet) and Phase
4C (additional sources). Phase 4B was subsequently implemented; see the
Phase 4B section above. Phase 4C's prerequisite (ranking ingestion) was
also subsequently implemented — see that section above. The real Yahoo/
ESPN/Athletic adapters remain not built; see the authoritative "Not yet
built" section at the bottom of this document for current status.

## Phase 3: ingestion pipeline
Screenshot -> Raw Observation -> Extraction -> Validation -> Confidence -> Auto-Accept/Review -> Trusted Statistic -> Fantasy Service

- `ingestion/extractor.py` — pluggable `Extractor` interface. `AnthropicVisionExtractor`
  is the real backend (requires `pip install anthropic` and an `ANTHROPIC_API_KEY`
  env var — this is a local app, it does not embed a key). If unavailable, the
  upload is still saved and the whole import is routed to the Review Queue
  rather than the request failing.
- `ingestion/fake_extractor.py` — deterministic test double; all pipeline logic
  is tested against this, independent of any AI provider.
- `ingestion/validation.py` — soft plausibility/week-consistency checks (never
  raise; they lower confidence and force review instead of hard-rejecting).
- `ingestion/pipeline.py` — orchestration only. Every write to `statistics`/
  `projections` goes through the exact same `db.repository.add_statistic` /
  `add_projection` functions manual entry uses, so it inherits versioning and
  conflict detection for free rather than a separate resolution system.
- New table: `proposed_observations` (one row per extracted stat field, with
  its own confidence, validation flags, and disposition). This is the explicit
  middle stage between a raw extraction and a trusted statistic — nothing is
  written directly into `statistics`/`projections` from extraction.
- Auto-accept requires ALL of: field confidence >= 0.85, an unambiguous
  resolved player identity (exact/alias match — never guessed or auto-created),
  a clean hard-validation pass, no plausibility/week-mismatch flags, and a
  known week. High confidence never overrides an unresolved or ambiguous
  identity.
- Review Queue extended: extraction proposals show per-field confidence,
  extracted player name, identity match type, and any flags; a human can
  accept as-is, accept with a corrected value, resolve identity by picking an
  existing player or creating a new one, or reject outright. Nothing here
  bypasses the existing conflict/versioning machinery.
- New page: `/ingest` (upload form: file, actual-vs-projection, source, week,
  optional position hint).
- 13 new pipeline tests (auto-accept, low confidence, unresolved/ambiguous
  identity, hard-invalid values, implausible values, week mismatches, raw-data
  immutability regardless of outcome, extractor-unavailable graceful failure,
  accept-with-correction, accept-with-new-player, reject, reused conflict
  detection, and projection vs actual routing) — all verified through the
  repository layer and through live HTTP requests against the Flask app.

## Phase 2 audit (previous update)
Before Phase 3, an audit pass found and fixed a real gap: rejecting a
conflict in the Review Queue previously had no effect on which statistic
value was trusted. Fixed via `repo.resolve_statistic_conflict()`. See
`tests/test_phase2_audit.py`. Also added a DB-level invariant (partial
unique index) guaranteeing only one source can ever be marked the canonical
actuals source, for deterministic source selection.

## Fantasy analytical layer
- `fantasy/scoring.py` — single authoritative PPR scoring config + pure `calculate_fantasy_points()`.
- `fantasy/service.py` — pulls trusted raw stats (current-version, canonical-source
  for actuals) and derives projected/actual FP, projected-vs-actual comparison,
  weekly view, week-over-week trend, and accuracy summary. Nothing here persists
  a fantasy-point value — every call recalculates from whatever raw data is
  currently trusted, so a correction to a raw stat changes the score automatically.
- New page: `/players/<id>` — weekly view, trend, accuracy summary for one player.
- Dashboard: new Fantasy Summary + Data Quality panel.
- New export: `/export/fantasy_points` (derived fields only — raw exports unchanged).
- Minimal data-integrity checks added in `db/repository.py` (non-negative
  receptions/TDs/fumbles/interceptions) — raised as `ValueError`, surfaced as a
  flash message in the UI, not a crash.
- `tests/` — 27 unit tests (scoring engine, fantasy service, and a regression
  suite for identity resolution/aliases/versioning/conflicts/rankings). Run with
  `python3 -m unittest discover -s tests -v`.

## Source status and canonical actuals
ESPN is the canonical actuals source: every actuals row on file (Weeks 1-6) was
ingested from ESPN, and the `is_canonical_actuals` flag was moved from the
placeholder `NFL` source to `ESPN` on 2026-10-19 with
`scripts/set_canonical_actuals_source.py` (dry run by default; `--apply` takes
a backup, changes only that flag in one transaction, and validates that exactly
one source is canonical and no statistics counts changed). Before that date the
flag pointed at `NFL`, which holds no actuals, so every projected-vs-actual
comparison returned no actual. Yahoo data is retained for comparison and
validation and is not promoted to canonical.

Background: `sources.is_canonical_actuals = 'yes'` can be set on only one source
at a time (a unique index, `idx_single_canonical_actuals_source`, guarantees at
most one canonical actuals source). Yahoo, ESPN, and
The Athletic are stored as separate, source-specific sources and are never
merged into a consensus.

Yahoo status: `ingestion/yahoo_weekly_adapter.py` (`YahooWeeklyPdfAdapter`)
exists and was used for the Week 2-6 weekly loads (`run_yahoo_week*_pull.py`).
It is a specimen-based path, not a live feed: it reads a JSON payload that
`scripts/parse_yahoo_weekly_playerlist.py` builds from Yahoo Player List PDFs
exported by hand. It is not registered in the scheduler's `ADAPTER_REGISTRY`,
has no dedicated tests in `tests/`, and its own docstring lists known gaps (no
QB completions or attempts, no targets or return stats, limited TE rushing).
There is no automated or live Yahoo acquisition. Earlier phase sections that
call the Yahoo adapters unbuilt reflect when they were written; the "Not yet
built" section at the end of this document is the current account.

## Limitations
- The app is local-first; it is not a hosted multi-user service.
- The scheduler logic is implemented, but external task scheduling is still
  required for unattended collection.
- No source adapter should be trusted beyond the real specimen and acquisition
  path it was tested against.
- Source comparisons remain source-specific; the app does not manufacture a
  consensus projection.

## Research/AAR value
The system's high-value extract is a source-level record of preseason and weekly
projections, actual statistics, rankings, observation provenance,
identity-review outcomes, conflicts, corrections, and collection-run health.
Keep these distinguishable by source and version rather than merging them into
an invented consensus. `db/tracker.db` is the operational evidence store; before
sharing a database or archive, remove private uploads, API credentials,
subscriber-source material, and anything you lack permission to redistribute.

## Recent changes (2026-09-07)
- **`stat_name` integrity:** `validate_stat_value(conn, stat_name, value)` now
  also checks `stat_name` against `stat_definitions`, so an unrecognized
  category (for example a bare `fantasy_points` total) can no longer
  auto-accept into the trusted `projections` table. Covers both the
  auto-accept and manual review-accept paths. Tests are in
  `tests/test_ingestion_pipeline.py`.
- **Per-source weekly accuracy:** `/players/<id>` previously defaulted to
  `source_name="User"`. It now renders one Accuracy Summary / Weekly View /
  Trend block per source via `get_weekly_projection_sources()` and
  `player_weekly_comparison_by_source()` in `fantasy/service.py`. Test is in
  `tests/test_fantasy_service.py`.

## Not yet built — authoritative current-state summary
This section supersedes any "Not built" notes inside individual phase
sections above, which reflect what was true only *at the time that phase
was written* and may since have been completed. This section is the one to trust.

**Current canonical status: Phase 4C. Adapters that exist today:
`AthleticSeasonXlsxAdapter` and `ESPNSeasonProjectionsAdapter` (season-long,
each built from a genuine specimen, tested, and registered with the
scheduler); `ESPNWeeklyLiveAdapter` (weekly projections and actuals from
ESPN's live `kona_player_info` endpoint; registered, with its parsing tested
against a captured specimen but no automated test of a successful live fetch);
and two specimen-based weekly adapters, `AthleticWeeklyXlsxAdapter` and
`YahooWeeklyPdfAdapter`, which read files you supply, are not registered with
the scheduler, and have no dedicated tests in `tests/`. ESPN editorial rankings
and The Athletic's live weekly rankings remain research-status; neither is on
file. There is no live or automated Yahoo acquisition (see "Source status and
canonical actuals" above). Statements in this section other than those about
adapters and rankings are as originally written and were not re-verified on
2026-10-17.**

- **Yahoo live or automated acquisition** — not built. Yahoo data enters only
  through the specimen-based weekly adapter described above, which is not
  registered with the scheduler and has no dedicated tests.
- **ESPN editorial rankings and The Athletic live weekly rankings
  acquisition** — not validated, as originally written. No fixtures were
  invented for them.
- **A real HTTP-based `SourceAdapter`** — two exist, both calling ESPN's live
  `kona_player_info` endpoint. `ESPNWeeklyLiveAdapter` (`ingestion/`) is
  registered with the scheduler and used by the `run_espn_week*_pull.py`
  scripts; its parsing is tested against a captured specimen, no automated test
  performs a successful live fetch, and that check is
  `scripts/live_check_espn_weekly.py`, outside the automated tests.
  `ESPNSourceAdapter` (the standalone `espn_adapter/` project) is not
  registered and is not used by the app or the pull scripts; it was verified
  live by hand (see `espn_adapter/FINDINGS.md`), and the automated tests in
  that package cover its fixture-based sibling `LocalFileSourceAdapter`, not
  its HTTP path. The original note that HTTP adapters were blocked by the
  authoring sandbox's network allowlist is historical. No HTTP-based adapter
  exists for Yahoo or The Athletic.
- **A UI page for season-scope (preseason/season-long) projections and
  rankings** — the data is fully stored, versioned, and queryable, but the
  dashboard and player-detail pages are still week-indexed and won't
  surface it yet.
- **Actually invoking the scheduler on a timer** (cron entry, background
  thread, etc.) — `Scheduler.run_due_jobs()` exists and is tested, but
  nothing in this app currently calls it automatically; that wiring is a
  couple of lines whenever you're ready (e.g. a cron job calling a small
  script that does `Scheduler(get_conn()).run_due_jobs()`).
- `pip install anthropic` is required for live screenshot extraction;
  `pip install openpyxl` is required for The Athletic xlsx adapter. Neither
  is needed for manual entry.

**The next meaningful action**: obtain one genuine captured ranking
specimen (browser network-tab capture or saved authenticated page) for
ESPN's editorial rankings or The Athletic's live weekly rankings — or provide
an updated copy of the season-long projections file to re-run the existing
adapter against.
