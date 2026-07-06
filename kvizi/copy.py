from __future__ import annotations

from collections.abc import Iterable

from kvizi.scoring import CHALLENGE_ECONOMY, DIFFICULTY_BASE_POINTS, challenge_cost, challenge_reward


DEFAULT_DIFFICULTY_ORDER = ("easy", "normal", "hard")


def ordered_difficulties(difficulties: Iterable[str]) -> list[str]:
    difficulty_set = {str(difficulty).strip().lower() for difficulty in difficulties if str(difficulty).strip()}
    standard = [difficulty for difficulty in DEFAULT_DIFFICULTY_ORDER if difficulty in difficulty_set]
    custom = sorted(difficulty_set - set(DEFAULT_DIFFICULTY_ORDER))
    return standard + custom


def rules_text(
    difficulty_points: dict[str, int] | None = None,
    challenge_economy: dict[str, dict[str, int]] | None = None,
) -> str:
    difficulty_points = difficulty_points or DIFFICULTY_BASE_POINTS
    challenge_economy = challenge_economy or CHALLENGE_ECONOMY
    difficulties = ordered_difficulties(difficulty_points)
    points_text = ", ".join(
        f"{difficulty} - {difficulty_points[difficulty]}"
        for difficulty in difficulties
    )
    challenge_text = ", ".join(
        f"{difficulty} {challenge_cost(difficulty, challenge_economy)}->{challenge_reward(difficulty, challenge_economy)}"
        for difficulty in difficulties
    )
    return (
        "Добро пожаловать в манеж Квизи!\n\n"
        f"Верный ответ приносит очки: {points_text}.\n"
        "Кнопки x2 и x3 повышают награду, но добавляют риск: ошибка на x2 снимает базу, "
        "ошибка на x3 снимает две базы. Счет ниже нуля не падает.\n"
        "Серии дают бонусы: +3 на третьем верном ответе подряд, +7 на пятом, +15 на десятом.\n"
        "Ставку надо нажать до ответа в опросе. После ответа аппарат уже щелкнул."
        "\n\n"
        "Табло: /top для общего рейтинга, /top <topic_key> для рейтинга сектора.\n"
        "\n"
        "Вызов: /kvizi_challenge <difficulty> внутри привязанного топика.\n"
        f"Вызовы: {challenge_text}.\n"
        "Если ошибся или не ответил до закрытия, стоимость вызова сгорает."
    )


RULES_TEXT = rules_text()


def config_text(
    difficulty_points: dict[str, int] | None = None,
    challenge_economy: dict[str, dict[str, int]] | None = None,
) -> str:
    difficulty_points = difficulty_points or DIFFICULTY_BASE_POINTS
    challenge_economy = challenge_economy or CHALLENGE_ECONOMY
    difficulties = ordered_difficulties(difficulty_points)
    lines = [
        "Конфиг Квизи:",
        "Очки за верный ответ:",
    ]
    lines.extend(f"- {difficulty}: {difficulty_points[difficulty]}" for difficulty in difficulties)
    lines.append("Вызовы:")
    lines.extend(
        (
            f"- {difficulty}: стоимость {challenge_cost(difficulty, challenge_economy)}, "
            f"награда +{challenge_reward(difficulty, challenge_economy)}"
        )
        for difficulty in difficulties
    )
    return "\n".join(lines)

ADMIN_HELP_TEXT = (
    "Админ-пульт Квизи:\n"
    "/kvizi_help_admin - эта справка\n"
    "/kvizi_bind <topic_key> <weight> - привязать текущий топик\n"
    "/kvizi_topics - список привязанных топиков\n"
    "/kvizi_config - текущий баланс очков и вызовов\n"
    "/kvizi_recent - последние вопросы и ответы\n"
    "/kvizi_errors - последние ошибки Telegram/cron\n"
    "/kvizi_status - подробный статус\n"
    "/kvizi_status_compact - короткий статус\n"
    "/kvizi_questions_status - покрытие questions.csv\n"
    "/kvizi_questions_template [difficulty] - CSV-шаблон вопросов\n"
    "/kvizi_postnow [topic_key] - отправить вопрос сейчас\n"
    "/kvizi_close_here - закрыть активные вопросы в текущем топике\n"
    "/kvizi_announce_here - назначить топик анонсов\n"
    "/kvizi_reload - перечитать questions.csv\n"
    "/kvizi_upload_questions [--check] - проверить или заменить questions.csv\n"
    "/kvizi_backups - список backup questions.csv\n"
    "/kvizi_restore_questions <n> - восстановить backup questions.csv\n"
    "/kvizi_export [--full] - выгрузить состояние JSON\n"
    "/kvizi_daily - отправить итоги дня сюда\n"
    "/kvizi_season_reset - сбросить текущий сезон\n"
    "\n"
    "Cron endpoints:\n"
    "POST /cron/tick - плановый вопрос\n"
    "POST /cron/maintenance - закрыть истёкшие poll\n"
    "POST /cron/daily - автоматические итоги дня\n"
    "POST /cron/backup - JSON backup админам\n"
    "\n"
    "Локально:\n"
    "python scripts/local_cron.py tick\n"
    "python scripts/local_cron.py maintenance\n"
    "python scripts/local_cron.py daily\n"
    "python scripts/local_cron.py backup"
)


def question_intro(topic_key: str, difficulty: str, base: int | None = None) -> str:
    base = DIFFICULTY_BASE_POINTS.get(difficulty, 10) if base is None else base
    return f"Квизи выкатывает вопрос в сектор {topic_key}! Сложность {difficulty}, база {base}."


def question_announcement(topic_key: str, difficulty: str, link: str, base: int | None = None) -> str:
    base = DIFFICULTY_BASE_POINTS.get(difficulty, 10) if base is None else base
    return (
        f"Квизи выкатывает вопрос в сектор {topic_key}! "
        f"Сложность {difficulty}, база {base}.\n"
        f"{link}"
    )


def bet_accepted(stake: int) -> str:
    return f"Ставка x{stake} принята. Шестеренки риска уже крутятся."


def bet_rejected(reason: str) -> str:
    return f"Ставка не прошла: {reason}"


def no_questions_text() -> str:
    return "Вопросов нет. Манеж пуст, прожекторы грустят."


def top_header(season: str) -> str:
    return f"Табло сезона {season}:"


def admin_only() -> str:
    return "Эта ручка только для администраторов Квизи."
