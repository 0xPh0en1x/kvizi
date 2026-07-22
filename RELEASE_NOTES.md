# Release Notes

## Reliability update - 2026-07-22

- Reconciled Telegram's `poll can't be stopped` response as an already closed
  poll instead of leaving it active and logging the same stop failure repeatedly.
- Added a durable SQLite retry queue for announcement messages that hit an
  ambiguous Telegram/proxy failure. Scheduled maintenance retries them without
  making quiz publication depend on announcement delivery. Telegram does not
  offer an idempotency key for `sendMessage`, so a lost success response can
  still produce a rare duplicate on delayed retry.
- Updated native quiz poll payloads for the current Telegram Bot API.
- Disabled automatic retries for non-idempotent Telegram sends and retained
  retries for safe metadata/download requests.
- Added durable SQLite claims for overlapping question and daily-summary runs.
- Made failed webhook updates retryable after any unexpected handler error.
- Removed built-in webhook and cron secrets; unauthenticated endpoints now fail closed.
- Made `/health` report real readiness with HTTP 503 on configuration or question-load errors.
- Added Telegram length-limit validation for questions, options, and explanations.
- Added GitHub Actions checks on pushes and pull requests for Python 3.10 and 3.13.
- Made Telegram poll state authoritative instead of rejecting answers solely by local time.
- Added a one-hour `closing` grace period for delayed `poll_answer` webhook delivery.
- Kept polls active after ambiguous `stopPoll` failures so a replacement poll is not posted.
- Added Telegram-side automatic poll closure through `open_period` and handling of `poll` updates.
- Added `poll_answer_rejected` diagnostics for answers that arrive after finalization.
- Extended answer delivery grace to 24 hours when Telegram and SQLite voter totals differ.
- Added Telegram-voter versus SQLite-answer mismatch diagnostics at poll finalization.
- Added Telegram/SQLite answer-delivery audit states to `/kvizi_recent` and a
  finalized-mismatch warning to `/kvizi_prod_check`.
- Excluded synthetic unanswered-challenge settlements from human answer counts.
- Split `/kvizi_errors` into a fresh 36-hour operational window and retained
  history that is explicitly marked as not affecting prod-check.
- Collapsed exact duplicate cron event/run pairs in the admin error report and
  prod-check.
- Replaced the scheduled limited JSON backup with a complete, integrity-checked
  SQLite snapshot that includes committed WAL state.
- Added a validation-first database restore tool with an explicit stopped-app
  confirmation and an automatic pre-restore backup.

## v1 local prerelease - 2026-07-06

Статус: готово к первому переносу на PythonAnywhere, но production-деплой ещё
не выполнен.

### Bot runtime

- Flask app with Telegram webhook, `/health`, `/cron/tick`, `/cron/maintenance`, and `/cron/daily`.
- Native Telegram quiz polls in forum topics through `message_thread_id`.
- SQLite state for users, topics, polls, answers, bets, scores, question history, settings, processed updates, and cron runs.
- SQLite WAL mode and `busy_timeout=5000` for safer webhook/cron overlap.
- Weighted topic routing with busy-topic skip for scheduled cron questions.

### Gameplay

- User commands: `/me`, `/top`, `/rules`, `/kvizi_challenge <difficulty>`.
- Admin commands for binding topics, manual posting, closing active polls, status, compact status, daily summary, export, season reset, and announcement topic.
- Scoring with base difficulty points, streak bonuses, x2/x3 bets, and non-negative score floor.
- Challenge questions with cost/reward and unanswered challenge settlement on poll close.
- Daily summary posting and idempotent `/cron/daily`.
- Automatic admin SQLite backup via `/cron/backup`.
- Topic-specific leaderboards via `/top <topic_key>`.
- Configurable difficulty points and challenge cost/reward via env.
- Cleaner `/rules` ordering and `/kvizi_config` for checking active scoring config.
- Production observability commands: `/kvizi_recent` and `/kvizi_errors`.
- Question quality review via `/kvizi_review`.
- Sharper persona copy variants for poll titles, announcements, bets, score events, and daily summaries: more irony, uneven rhythm, and theatrical Kvizi flavor.
- Admin voice smoke command `/kvizi_voice_preview` for checking current Kvizi copy without posting polls or changing scores.
- Public `/kvizi_help`, grouped `/kvizi_help_admin`, and `/kvizi_prod_check` for quick production readiness checks.
- Admin `/kvizi_version` command for checking git commit, code version, deploy paths, and loaded question count.
- Compact `/kvizi_errors` output and transient Telegram/proxy classification so temporary 503/proxy failures do not turn prod-check into WARN.
- Compact `/kvizi_prod_check` cron lines by hiding stored cron messages from the readiness summary.
- Season leader change announcements in the configured announcement topic.
- Streak milestone announcements for series bonuses in the configured announcement topic.
- Risk-failure announcements for wrong x2/x3 answers in the configured announcement topic.
- Live announcement-topic reactions for polls closed without answers and for the first answer of the day.
- Env feature flags for noisy announcement-topic reactions: first answer, no-answer closes, risk failures, and streaks.

### Question operations

- CSV validation with topic/difficulty coverage, duplicate id checks, and binding warnings.
- Telegram `questions.csv` upload with `--check` dry-run, validation before replace, backup creation, reload, and report.
- Backup listing and restore commands for `questions.csv`.
- CSV template generation through `/kvizi_questions_template [difficulty]`.

### Local and deployment tooling

- `scripts/local_cron.py` for local cron endpoint runs.
- `scripts/smoke_check.py` for pre-deploy smoke checks.
- `PROD_CHECKLIST.md` for quick post-pull PythonAnywhere verification.
- `.env.example` with deploy env vars.
- `DEPLOY.md` with PythonAnywhere, Telegram webhook, and cron-job.org checklist.

### Verification

Last local verification:

```text
python -m pytest -q -> 118 passed
python scripts/validate_questions.py -> OK, warnings expected for sample CSV
python scripts/smoke_check.py -> failures=0, warnings expected locally
```
