# Квизи

Квизи — Flask-бот для Telegram forum group topics. cron-job.org дергает
`/cron/tick`, бот выбирает топик по весам, отправляет quiz poll, принимает
ставки x2/x3 через inline-кнопки и считает очки по `poll_answer`.

## Быстрый старт локально

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/validate_questions.py
python scripts/init_db.py
flask --app app run
```

Локально cron-ручки можно дергать без HTTP-клиента:

```powershell
python scripts/local_cron.py tick
python scripts/local_cron.py maintenance
python scripts/local_cron.py daily
```

Перед деплоем можно прогнать smoke-check:

```powershell
python scripts/smoke_check.py
```

Подробный чеклист деплоя: `DEPLOY.md`.

Предрелизный список возможностей: `RELEASE_NOTES.md`.

Минимальные переменные окружения для реального запуска:

```powershell
$env:TELEGRAM_BOT_TOKEN="123:token"
$env:TELEGRAM_CHAT_ID="-1001234567890"
$env:KVIZI_WEBHOOK_SECRET="long-random-secret"
$env:KVIZI_CRON_SECRET="another-long-random-secret"
$env:KVIZI_ADMIN_IDS="111111111,222222222"
```

Дополнительно:

- `KVIZI_TZ=Europe/Moscow`
- `KVIZI_OPEN_SECONDS=7200`
- `KVIZI_DB_PATH=.../kvizi.sqlite3`
- `KVIZI_QUESTIONS_PATH=.../questions.csv`
- `KVIZI_SEASON=main`
- `KVIZI_ANNOUNCE_THREAD_ID=123` — необязательно, можно задать командой
- `KVIZI_CHAT_USERNAME=my_public_group` — если группа публичная и нужны публичные ссылки

## Вопросы

Вопросы лежат в `questions.csv`. `correct_option_ids` в v1 — один номер
варианта от `1` до `6`, потому что Telegram quiz poll поддерживает один
правильный ответ.

Колонки:

```text
id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,option_5,option_6,correct_option_ids,explanation,source
```

`difficulty` — slug: `easy`, `normal`, `hard`, `ccna`, `linux_basics` и т.п.
Для обычных очков известны базы `easy=5`, `normal=10`, `hard=15`; неизвестные
slug пока считаются как `normal`.

`python scripts/validate_questions.py` проверяет CSV и печатает покрытие:
статистику по `topic_key`, `topic_key + difficulty`, duplicate id, темы из CSV
без привязки в SQLite и привязанные топики без вопросов. Нехватка вопросов или
сложностей выводится как warning и не ломает запуск.

### Загрузка вопросов через Telegram

Админ может заменить `questions.csv` прямо из Telegram:

1. Прикрепить новый CSV как документ.
2. В caption документа написать:

```text
/kvizi_upload_questions
```

Для dry-run проверки без замены файла:

```text
/kvizi_upload_questions --check
```

Бот скачает файл, проверит CSV, создаст backup текущего файла в `backups/`,
заменит `questions.csv`, перечитает вопросы и отправит отчёт как
`/kvizi_questions_status`. Если CSV сломан или пустой, текущий файл не
заменяется. В режиме `--check` backup не создаётся, файл не заменяется и банк
вопросов не перечитывается.

Посмотреть последние backup:

```text
/kvizi_backups
```

Восстановить backup по номеру из списка:

```text
/kvizi_restore_questions 1
```

Перед восстановлением backup валидируется, а текущий `questions.csv` сохраняется
новым backup-файлом.

Получить CSV-шаблон для заполнения:

```text
/kvizi_questions_template
/kvizi_questions_template ccna
```

Если есть привязанные топики, шаблон строится по ним. Если привязок нет, берутся
topic из текущего CSV.

## Telegram команды

Пользовательские:

- `/me` — личный счет
- `/top` — таблица лидеров
- `/rules` — правила очков, серий и ставок
- `/kvizi_challenge <difficulty>` — вызвать вопрос выбранной сложности в текущем привязанном топике

Админские:

- `/kvizi_help_admin` — короткая справка по админ-командам и cron-ручкам
- `/kvizi_bind <topic_key> <weight>` — выполнить внутри нужного топика
- `/kvizi_status` — показать вопросы, топики, активные poll/challenge и последний cron
- `/kvizi_status_compact` — короткий статус со счётчиками active/expired/challenge
- `/kvizi_questions_status` — показать покрытие `questions.csv` по темам и сложностям
- `/kvizi_questions_template [difficulty]` — отправить CSV-шаблон для новых вопросов
- `/kvizi_close_here` — закрыть все активные вопросы в текущем топике
- `/kvizi_export` — отправить JSON-экспорт состояния файлом в текущий топик
- `/kvizi_daily` — вручную отправить итоги дня в текущий топик
- `/kvizi_upload_questions [--check]` — проверить или заменить `questions.csv` прикреплённым CSV-документом
- `/kvizi_backups` — показать последние backup-файлы `questions.csv`
- `/kvizi_restore_questions <n>` — восстановить `questions.csv` из backup по номеру
- `/kvizi_topics` — список привязанных топиков
- `/kvizi_reload` — перечитать CSV
- `/kvizi_postnow [topic_key]` — сразу отправить вопрос
- `/kvizi_season_reset` — сбросить текущий сезон
- `/kvizi_announce_here` — назначить текущий топик топиком анонсов

## Анонс-топик

Создай отдельный topic, зайди в него админом и напиши:

```text
/kvizi_announce_here
```

После этого каждый новый вопрос будет сопровождаться сообщением в этом топике:

```text
Квизи выкатывает вопрос в сектор network! Сложность normal, база 10.
https://t.me/c/1234567890/456
```

Чтобы в этом топике писал только Квизи, ограничь его на стороне Telegram:
оставь право писать сообщения только администраторам/боту или закрой топик для
обычных участников, если этот режим доступен в твоем клиенте.

## PythonAnywhere

1. Загрузить проект.
2. Создать virtualenv и установить `pip install -r requirements.txt`.
3. В Web app WSGI-файле импортировать `application` из `wsgi.py`.
4. Указать env vars в настройках/WSGI.
5. Инициализировать базу:

```bash
python scripts/init_db.py
python scripts/validate_questions.py
```

6. Настроить webhook:

```bash
python scripts/set_webhook.py --base-url https://YOUR_USERNAME.pythonanywhere.com --drop-pending
```

## Экспорт состояния

Сохранить read-only JSON-снимок текущей SQLite:

```bash
python scripts/export_state.py
```

По умолчанию файл создаётся в `exports/` и не попадает в git. Можно указать путь:

```bash
python scripts/export_state.py --output backup/kvizi-state.json
```

Экспорт включает топики, пользователей, очки, активные poll, последние ответы,
ставки, историю вопросов, настройки бота и последние cron-запуски.

Админ может запросить тот же экспорт через Telegram:

```text
/kvizi_export
```

Технические `processed_updates` по умолчанию не включаются. Полная выгрузка:

```text
/kvizi_export --full
```

## cron-job.org

Создать POST job на:

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/tick
```

Header:

```text
X-Kvizi-Cron-Secret: <KVIZI_CRON_SECRET>
```

Частота v1: 3-5 запусков в день.

Если в подходящем топике уже есть активный вопрос, cron-запуск не отправляет
туда новый poll. Если все подходящие топики заняты, `/cron/tick` возвращает
`posted=false`.

Для обслуживания истёкших poll создать отдельный частый POST job:

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/maintenance
```

Header тот же:

```text
X-Kvizi-Cron-Secret: <KVIZI_CRON_SECRET>
```

Рекомендуемая частота: раз в 10-15 минут. `/cron/maintenance` не публикует
новые вопросы и не отправляет итоги дня; он только закрывает истёкшие poll,
фиксирует просроченные challenge и пишет результат в `cron_runs`.

Для автоматических итогов дня создать отдельный POST job, например вечером по
`Europe/Moscow`:

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/daily
```

Header тот же:

```text
X-Kvizi-Cron-Secret: <KVIZI_CRON_SECRET>
```

`/cron/daily` идемпотентен по локальной дате: если итоги за день уже отправлены,
повторный вызов вернёт `posted=false` и не продублирует сообщение.

Для автоматического JSON backup можно создать отдельный POST job:

```text
https://YOUR_USERNAME.pythonanywhere.com/cron/backup
```

Он отправляет export состояния каждому `user_id` из `KVIZI_ADMIN_IDS`. Админ
должен заранее открыть личный чат с ботом, иначе Telegram не даст боту начать
диалог и этот конкретный admin id попадёт в `errors` ответа cron.

SQLite при `init_db` переводится в WAL-режим, а каждое соединение получает
`busy_timeout=5000`, чтобы webhook и cron реже конфликтовали при одновременных
записях.

## Счет

- easy: 5, normal: 10, hard: 15
- correct: `base * stake + streak_bonus`
- wrong x1: `0`
- wrong x2: `-base`
- wrong x3: `-2 * base`
- счет не падает ниже `0`
- streak bonuses: `+3` на 3 подряд, `+7` на 5 подряд, `+15` на 10 подряд

Ставка засчитывается только если нажата до ответа в poll.

## Вызовы за очки

Игрок может вызвать вопрос нужной сложности командой в привязанном топике:

```text
/kvizi_challenge normal
```

Правила v1:

- easy: нужно 5 очков, правильный ответ дает +10
- normal: нужно 10 очков, правильный ответ дает +25
- hard: нужно 15 очков, правильный ответ дает +40
- неизвестные difficulty-slug, например `ccna`, используют баланс normal: нужно 10, награда +25
- если игрок отвечает неправильно или не отвечает до закрытия poll, он теряет стоимость вызова
- у игрока может быть только один активный вызов
- x2/x3 для автора вызова отключены; остальные участники могут отвечать и ставить как обычно

Ручные `/kvizi_postnow` и `/kvizi_challenge` не создают новый вопрос, если в
выбранном топике уже есть активный poll. Админ может закрыть зависшие вопросы
в текущем топике командой `/kvizi_close_here`.
