# Release Notes

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
- Automatic admin JSON backup via `/cron/backup`.
- Topic-specific leaderboards via `/top <topic_key>`.
- Configurable difficulty points and challenge cost/reward via env.
- Cleaner `/rules` ordering and `/kvizi_config` for checking active scoring config.
- Production observability commands: `/kvizi_recent` and `/kvizi_errors`.
- Question quality review via `/kvizi_review`.
- Sharper persona copy variants for poll titles, announcements, bets, score events, and daily summaries: more irony, uneven rhythm, and theatrical Kvizi flavor.
- Admin voice smoke command `/kvizi_voice_preview` for checking current Kvizi copy without posting polls or changing scores.
- Public `/kvizi_help`, grouped `/kvizi_help_admin`, and `/kvizi_prod_check` for quick production readiness checks.
- Compact `/kvizi_errors` output and transient Telegram/proxy classification so temporary 503/proxy failures do not turn prod-check into WARN.
- Compact `/kvizi_prod_check` cron lines by hiding stored cron messages from the readiness summary.
- Season leader change announcements in the configured announcement topic.
- Streak milestone announcements for series bonuses in the configured announcement topic.
- Risk-failure announcements for wrong x2/x3 answers in the configured announcement topic.

### Question operations

- CSV validation with topic/difficulty coverage, duplicate id checks, and binding warnings.
- Telegram `questions.csv` upload with `--check` dry-run, validation before replace, backup creation, reload, and report.
- Backup listing and restore commands for `questions.csv`.
- CSV template generation through `/kvizi_questions_template [difficulty]`.

### Local and deployment tooling

- `scripts/local_cron.py` for local cron endpoint runs.
- `scripts/smoke_check.py` for pre-deploy smoke checks.
- `.env.example` with deploy env vars.
- `DEPLOY.md` with PythonAnywhere, Telegram webhook, and cron-job.org checklist.

### Verification

Last local verification:

```text
python -m pytest -q -> 74 passed
python scripts/validate_questions.py -> OK, warnings expected for sample CSV
python scripts/smoke_check.py -> failures=0, warnings expected locally
```
