# Archived one-off weekly audit scripts

Historical, single-use scripts from the Weeks 1-6 data loads (review-queue
resolution, diagnostics, dumps, one-time fixes). Moved here on 2026-10-17
with `git mv`; contents are unchanged, so `git log --follow <file>` shows full history.

- **Not runnable in place.** They import `db.*` and use repo-root-relative paths.
  To re-run one, copy it back to the repo root.
- Groups: `resolve_*` (12 review-queue resolutions), `dump_*` (7 read-only dumps),
  and one-offs: `diagnose_week4_lock_gap`, `verify_week4_actuals`,
  `finish_freeze_checklist`, `lock_athletic_week1`, `fix_and_resolve_yahoo_week3`,
  `reject_yahoo_preseason_rank_conflicts`, `auto_cleanup`.
- `auto_cleanup.py` is archived, not deleted, pending a later decision.
- Still at the repo root: `app.py`, the lock-gap migration and validator, and the `run_*_weekN_pull.py` scripts.
