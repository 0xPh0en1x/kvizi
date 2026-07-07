# Kvizi Deployment

Короткий чеклист для первого запуска на PythonAnywhere. Команды выполнять из
корня проекта.

## 1. Локальная проверка

```bash
python -m pytest -q
python scripts/validate_questions.py
python scripts/smoke_check.py
```

Перед реальным деплоем с заполненными переменными:

```bash
python scripts/smoke_check.py --strict-env
```

## 2. Переменные окружения

Скопировать `.env.example` как рабочий шаблон и заполнить реальные значения:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-100...
KVIZI_WEBHOOK_SECRET=long-random-secret
KVIZI_CRON_SECRET=another-long-random-secret
KVIZI_ADMIN_IDS=111111111,222222222
KVIZI_TZ=Europe/Moscow
KVIZI_OPEN_SECONDS=7200
KVIZI_DB_PATH=/home/YOUR_USERNAME/kvizi/data/kvizi.sqlite3
KVIZI_QUESTIONS_PATH=/home/YOUR_USERNAME/kvizi/questions.csv
KVIZI_SEASON=main
KVIZI_DIFFICULTY_POINTS=easy:5,normal:10,hard:15,ccna:20
KVIZI_CHALLENGE_REWARDS=easy:5:10,normal:10:25,hard:15:40,ccna:20:55
```

На PythonAnywhere задай эти переменные в Web app environment, если этот способ
доступен. Если нет, добавь их в WSGI-файл перед импортом приложения:

```python
import os
import sys

project_path = "/home/YOUR_USERNAME/kvizi"
if project_path not in sys.path:
    sys.path.insert(0, project_path)

os.environ["TELEGRAM_BOT_TOKEN"] = "..."
os.environ["TELEGRAM_CHAT_ID"] = "-100..."
os.environ["KVIZI_WEBHOOK_SECRET"] = "..."
os.environ["KVIZI_CRON_SECRET"] = "..."
os.environ["KVIZI_ADMIN_IDS"] = "111111111,222222222"
os.environ["KVIZI_DB_PATH"] = "/home/YOUR_USERNAME/kvizi/data/kvizi.sqlite3"
os.environ["KVIZI_QUESTIONS_PATH"] = "/home/YOUR_USERNAME/kvizi/questions.csv"
os.environ["KVIZI_DIFFICULTY_POINTS"] = "easy:5,normal:10,hard:15,ccna:20"
os.environ["KVIZI_CHALLENGE_REWARDS"] = "easy:5:10,normal:10:25,hard:15:40,ccna:20:55"

from wsgi import application
```

## 3. PythonAnywhere setup

1. Upload/clone project to `/home/YOUR_USERNAME/kvizi`.
2. Create virtualenv and install dependencies:

```bash
cd /home/YOUR_USERNAME/kvizi
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. In PythonAnywhere Web tab:

- source code: `/home/YOUR_USERNAME/kvizi`
- WSGI file imports `application` from `wsgi.py`
- virtualenv: `/home/YOUR_USERNAME/kvizi/.venv`

4. Initialize and check:

```bash
python scripts/init_db.py
python scripts/validate_questions.py
python scripts/smoke_check.py --strict-env
```

5. Reload the web app.
6. Open:

```text
https://YOUR_USERNAME.pythonanywhere.com/health
```

It should return JSON with `"ok": true` and empty `load_error`.

## 4. Telegram setup

1. Add bot to the forum supergroup.
2. Give the bot permission to post messages and polls.
3. If document upload commands do not reach the bot, disable privacy mode in
   BotFather for this bot.
4. Set webhook:

```bash
python scripts/set_webhook.py --base-url https://YOUR_USERNAME.pythonanywhere.com --drop-pending
```

Allowed updates are configured by the script:

```text
message, callback_query, poll, poll_answer, my_chat_member
```

## 5. Bind Telegram topics

Run inside each target forum topic:

```text
/kvizi_bind network 3
/kvizi_bind security 2
/kvizi_bind system 1
```

Run inside the announcement topic:

```text
/kvizi_announce_here
```

Useful checks:

```text
/kvizi_help_admin
/kvizi_version
/kvizi_status_compact
/kvizi_questions_status
/kvizi_postnow
```

## 6. cron-job.org

Every job must be `POST` and include:

```text
X-Kvizi-Cron-Secret: <KVIZI_CRON_SECRET>
```

Jobs:

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/tick
```

3-5 times per day. Posts scheduled questions.

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/maintenance
```

Every 10-15 minutes. Closes expired polls and settles expired challenge.

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/daily
```

Once per day in the evening by `Europe/Moscow`. Posts daily summary once per
local date.

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/backup
```

Once per day or every few days. Sends JSON state export to every user id from
`KVIZI_ADMIN_IDS`. Each admin must open a private chat with the bot first,
otherwise Telegram will reject that delivery while other admins can still receive
the backup.

## 7. Rollback and maintenance

Question CSV workflow:

```text
/kvizi_upload_questions --check
/kvizi_upload_questions
/kvizi_backups
/kvizi_restore_questions 1
```

Manual state export:

```text
/kvizi_export
/kvizi_export --full
```

CLI export:

```bash
python scripts/export_state.py
```
