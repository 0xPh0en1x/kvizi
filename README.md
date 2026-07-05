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

## Telegram команды

Пользовательские:

- `/me` — личный счет
- `/top` — таблица лидеров
- `/rules` — правила очков, серий и ставок
- `/kvizi_challenge <difficulty>` — вызвать вопрос выбранной сложности в текущем привязанном топике

Админские:

- `/kvizi_bind <topic_key> <weight>` — выполнить внутри нужного топика
- `/kvizi_status` — показать вопросы, топики, активные poll/challenge и последний cron
- `/kvizi_close_here` — закрыть все активные вопросы в текущем топике
- `/kvizi_export` — отправить JSON-экспорт состояния файлом в текущий топик
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
