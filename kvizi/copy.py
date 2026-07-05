from __future__ import annotations

from kvizi.scoring import DIFFICULTY_BASE_POINTS


RULES_TEXT = (
    "Добро пожаловать в манеж Квизи!\n\n"
    "Верный ответ приносит очки: easy - 5, normal - 10, hard - 15.\n"
    "Кнопки x2 и x3 повышают награду, но добавляют риск: ошибка на x2 снимает базу, "
    "ошибка на x3 снимает две базы. Счет ниже нуля не падает.\n"
    "Серии дают бонусы: +3 на третьем верном ответе подряд, +7 на пятом, +15 на десятом.\n"
    "Ставку надо нажать до ответа в опросе. После ответа аппарат уже щелкнул."
    "\n\n"
    "Вызов: /kvizi_challenge <difficulty> внутри привязанного топика.\n"
    "easy стоит 5 и дает +10, normal стоит 10 и дает +25, hard стоит 15 и дает +40.\n"
    "Если ошибся или не ответил до закрытия, стоимость вызова сгорает."
)


def question_intro(topic_key: str, difficulty: str) -> str:
    base = DIFFICULTY_BASE_POINTS.get(difficulty, 10)
    return f"Квизи выкатывает вопрос в сектор {topic_key}! Сложность {difficulty}, база {base}."


def question_announcement(topic_key: str, difficulty: str, link: str) -> str:
    base = DIFFICULTY_BASE_POINTS.get(difficulty, 10)
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
