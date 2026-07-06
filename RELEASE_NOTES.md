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
python -m pytest -q -> 44 passed
python scripts/validate_questions.py -> OK, warnings expected for sample CSV
python scripts/smoke_check.py -> failures=0, warnings expected locally
```

