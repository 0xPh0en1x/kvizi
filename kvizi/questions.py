from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path


QUESTION_COLUMNS = [
    "id",
    "topic_key",
    "difficulty",
    "question",
    "option_1",
    "option_2",
    "option_3",
    "option_4",
    "option_5",
    "option_6",
    "correct_option_ids",
    "explanation",
    "source",
]

DIFFICULTY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_QUESTION_LENGTH = 300
MAX_OPTION_LENGTH = 100
MAX_EXPLANATION_LENGTH = 200
MAX_EXPLANATION_LINE_FEEDS = 2


class QuestionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    id: str
    topic_key: str
    difficulty: str
    text: str
    options: tuple[str, ...]
    correct_option_id: int
    explanation: str
    source: str


class QuestionBank:
    def __init__(self, questions: list[Question]) -> None:
        self._questions = questions
        self._by_topic: dict[str, list[Question]] = {}
        self._by_id: dict[str, Question] = {}
        for question in questions:
            self._by_topic.setdefault(question.topic_key, []).append(question)
            self._by_id[question.id] = question

    @property
    def questions(self) -> list[Question]:
        return list(self._questions)

    def count(self) -> int:
        return len(self._questions)

    def topics(self) -> set[str]:
        return set(self._by_topic)

    def difficulties(self, topic_key: str | None = None) -> set[str]:
        if topic_key is None:
            return {question.difficulty for question in self._questions}
        return {question.difficulty for question in self._by_topic.get(topic_key, [])}

    def by_topic(self, topic_key: str) -> list[Question]:
        return list(self._by_topic.get(topic_key, []))

    def get(self, question_id: str) -> Question | None:
        return self._by_id.get(question_id)


def load_questions(path: Path) -> QuestionBank:
    if not path.exists():
        return QuestionBank([])

    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            return QuestionBank([])

        missing = [column for column in QUESTION_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise QuestionValidationError(f"Missing columns: {', '.join(missing)}")

        questions: list[Question] = []
        seen_ids: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            questions.append(_parse_row(row, line_number, seen_ids))

    return QuestionBank(questions)


def _parse_row(row: dict[str, str], line_number: int, seen_ids: set[str]) -> Question:
    question_id = _required(row, "id", line_number)
    if question_id in seen_ids:
        raise QuestionValidationError(f"Line {line_number}: duplicate id {question_id!r}")
    seen_ids.add(question_id)

    topic_key = _required(row, "topic_key", line_number).strip().lower()
    difficulty = _required(row, "difficulty", line_number).strip().lower()
    if not DIFFICULTY_PATTERN.match(difficulty):
        raise QuestionValidationError(
            f"Line {line_number}: difficulty must be a slug like normal, hard, ccna"
        )

    text = _required(row, "question", line_number).strip()
    if len(text) > MAX_QUESTION_LENGTH:
        raise QuestionValidationError(
            f"Line {line_number}: question exceeds Telegram limit of {MAX_QUESTION_LENGTH} characters"
        )
    options = tuple(
        value.strip()
        for value in [row.get(f"option_{index}", "") for index in range(1, 7)]
        if value and value.strip()
    )
    if len(options) < 2:
        raise QuestionValidationError(f"Line {line_number}: at least two options are required")
    if len(options) > 10:
        raise QuestionValidationError(f"Line {line_number}: Telegram supports at most ten options")
    for option_index, option in enumerate(options, start=1):
        if len(option) > MAX_OPTION_LENGTH:
            raise QuestionValidationError(
                f"Line {line_number}: option_{option_index} exceeds Telegram limit of "
                f"{MAX_OPTION_LENGTH} characters"
            )

    correct_raw = _required(row, "correct_option_ids", line_number)
    correct_values = [item.strip() for item in correct_raw.replace(";", ",").split(",") if item.strip()]
    if len(correct_values) != 1:
        raise QuestionValidationError(
            f"Line {line_number}: quiz mode supports exactly one correct option"
        )
    try:
        correct_human_index = int(correct_values[0])
    except ValueError as exc:
        raise QuestionValidationError(
            f"Line {line_number}: correct_option_ids must contain a number from 1 to {len(options)}"
        ) from exc

    if correct_human_index < 1 or correct_human_index > len(options):
        raise QuestionValidationError(
            f"Line {line_number}: correct option must be from 1 to {len(options)}"
        )

    explanation = (row.get("explanation") or "").strip()
    if len(explanation) > MAX_EXPLANATION_LENGTH:
        raise QuestionValidationError(
            f"Line {line_number}: explanation exceeds Telegram limit of "
            f"{MAX_EXPLANATION_LENGTH} characters"
        )
    if explanation.count("\n") > MAX_EXPLANATION_LINE_FEEDS:
        raise QuestionValidationError(
            f"Line {line_number}: explanation exceeds Telegram limit of "
            f"{MAX_EXPLANATION_LINE_FEEDS} line feeds"
        )

    return Question(
        id=question_id,
        topic_key=topic_key,
        difficulty=difficulty,
        text=text,
        options=options,
        correct_option_id=correct_human_index - 1,
        explanation=explanation,
        source=(row.get("source") or "").strip(),
    )


def _required(row: dict[str, str], column: str, line_number: int) -> str:
    value = row.get(column)
    if value is None or not value.strip():
        raise QuestionValidationError(f"Line {line_number}: {column} is required")
    return value
