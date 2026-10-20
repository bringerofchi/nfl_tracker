# Week 4 lock-gap migration records (2026-10-07)

Sanitized copies of the five run records for the migration that froze 67
late-arriving Week 4 projection rows (ESPN 46, Yahoo 21).

- **Redacted.** Per-player projection values (6+ decimal numbers and
  per-player spot-check lines) are replaced with `<redacted>` or
  `{per-player projection values redacted}`. Counts, `[PASS]` results, the
  projected_value SHA-256 fingerprint and the aggregate sum are kept.
- The unredacted originals stay local and untracked (`out_*.txt`, ignored).
- Pre-migration backup (local, untracked): `db/tracker.db.bak-20261007-pre-lockgap`.
