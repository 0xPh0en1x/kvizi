from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Iterable

from kvizi.database import KviziRepository
from kvizi.questions import QuestionBank


MIN_QUESTIONS_PER_TOPIC = 3
STANDARD_DIFFICULTIES = ("easy", "normal", "hard")
TELEGRAM_REPORT_LIMIT = 3900


def build_report(
    bank: QuestionBank,
    duplicate_ids: list[str] | None = None,
    bound_topics: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    duplicate_ids = duplicate_ids or []
    topics = sorted(bank.topics())
    lines = [
        f"Questions OK: {bank.count()}",
        f"Duplicate ids: {', '.join(duplicate_ids) if duplicate_ids else 'none'}",
        f"Topics: {', '.join(topics) if topics else 'none'}",
        f"Difficulties: {format_counter(question.difficulty for question in bank.questions)}",
        "By topic:",
    ]

    warnings: list[str] = []
    if not topics:
        lines.append("- none")

    for topic_key in topics:
        questions = bank.by_topic(topic_key)
        difficulties = Counter(question.difficulty for question in questions)
        missing_standard = [
            difficulty
            for difficulty in STANDARD_DIFFICULTIES
            if difficulty not in difficulties
        ]
        suffix = (
            f" | missing standard: {', '.join(missing_standard)}"
            if missing_standard
            else ""
        )
        lines.append(
            f"- {topic_key}: total={len(questions)} | {format_counter(difficulties)}{suffix}"
        )
        if len(questions) < MIN_QUESTIONS_PER_TOPIC:
            warnings.append(
                f"topic {topic_key} has only {len(questions)} questions "
                f"(< {MIN_QUESTIONS_PER_TOPIC})"
            )
        if missing_standard:
            warnings.append(
                f"topic {topic_key} missing standard difficulties: {', '.join(missing_standard)}"
            )

    if bound_topics is None:
        lines.append("Bindings: SQLite database not found, skipped topic binding checks.")
    else:
        lines.append(
            "Bindings: "
            f"SQLite topics={', '.join(sorted(bound_topics)) if bound_topics else 'none'}"
        )
        csv_not_bound = sorted(set(topics) - bound_topics)
        bound_without_questions = sorted(bound_topics - set(topics))
        if csv_not_bound:
            warnings.append(f"CSV topics not bound in SQLite: {', '.join(csv_not_bound)}")
        if bound_without_questions:
            warnings.append(
                f"SQLite bound topics without questions: {', '.join(bound_without_questions)}"
            )

    return lines, warnings


def format_report_for_telegram(
    lines: list[str],
    warnings: list[str],
    *,
    warning_limit: int = 8,
) -> str:
    report_lines = ["Статус questions.csv:"]
    report_lines.extend(lines)

    if warnings:
        report_lines.append(f"Warnings: {len(warnings)}")
        for warning in warnings[:warning_limit]:
            report_lines.append(f"- {warning}")
        if len(warnings) > warning_limit:
            report_lines.append(f"- ... ещё {len(warnings) - warning_limit}")
    else:
        report_lines.append("Warnings: none")

    text = "\n".join(report_lines)
    if len(text) <= TELEGRAM_REPORT_LIMIT:
        return text
    return text[: TELEGRAM_REPORT_LIMIT - 20].rstrip() + "\n... truncated"


def find_duplicate_ids(path: Path) -> list[str]:
    if not path.exists():
        return []

    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None or "id" not in reader.fieldnames:
            return []
        ids = Counter(
            str(row.get("id") or "").strip()
            for row in reader
            if str(row.get("id") or "").strip()
        )
    return sorted(question_id for question_id, count in ids.items() if count > 1)


def load_bound_topic_keys(database_path: Path) -> set[str] | None:
    if not database_path.exists():
        return None
    repository = KviziRepository(database_path)
    return {str(topic["topic_key"]) for topic in repository.list_topics()}


def format_counter(values: Iterable[str] | Counter[str]) -> str:
    counter = values if isinstance(values, Counter) else Counter(values)
    if not counter:
        return "none"
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter))
