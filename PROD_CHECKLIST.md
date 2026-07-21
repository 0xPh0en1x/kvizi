# Kvizi Production Checklist

Use this after every deploy, pull, env change, or cron schedule change on PythonAnywhere.

## 1. Pull Code

First confirm that the latest commit has a green `CI` workflow in GitHub Actions.

```bash
cd ~/kvizi
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/validate_questions.py
python scripts/smoke_check.py
```

Expected:

- `validate_questions.py` reports `Questions OK`.
- `smoke_check.py` reports `failures=0`.
- If env vars are available in the Bash console, additionally run
  `python scripts/smoke_check.py --strict-env` and expect `failures=0`.
- If env vars exist only in the WSGI file, missing-env warnings in the Bash smoke
  check are expected; `/health` after reload is the authoritative readiness check.

## 2. Reload PythonAnywhere

In PythonAnywhere Web tab:

1. Check WSGI env vars if they changed.
2. Click `Reload`.
3. Open `/health`.

Expected `/health`:

```json
{"configuration_ok":true,"ok":true,"questions":75,"questions_loaded":true}
```

Any missing runtime setting, question-load failure, or empty question bank returns HTTP 503.

## 3. Telegram Smoke

Run in the admin topic:

```text
/kvizi_version
/kvizi_prod_check
/kvizi_status_compact
/kvizi_errors
```

Expected:

- `/kvizi_version` shows the expected git commit and `question_count`.
- `/kvizi_prod_check` is `OK` or only has understood transient Telegram/proxy info.
- `/kvizi_status_compact` shows active topics and the announcement topic.
- `/kvizi_errors` has no fresh non-transient errors.

If questions.csv was changed:

```text
/kvizi_questions_status
/kvizi_review
```

## 4. Cron Smoke

In cron-job.org, run test runs for:

- `Kvizi questions` -> `POST /cron/tick`
- `Kvizi maintenance` -> `POST /cron/maintenance`
- `Kvizi daily` -> `POST /cron/daily`
- `Kvizi auto backup` -> `POST /cron/backup`

Expected:

- HTTP `200 OK`.
- `maintenance` can close `0` polls.
- `daily` can skip if today's summary was already sent.
- `backup` sends JSON to admins who opened a private chat with the bot.

After test runs, check:

```text
/kvizi_prod_check
/kvizi_errors
```

## 5. Safe Rollback

Code rollback:

```bash
cd ~/kvizi
git log --oneline -5
git checkout <known_good_commit>
```

Then reload PythonAnywhere and run the Telegram smoke section again.

Questions rollback:

```text
/kvizi_backups
/kvizi_restore_questions <number>
/kvizi_questions_status
```

State backup:

```text
/kvizi_export
```
