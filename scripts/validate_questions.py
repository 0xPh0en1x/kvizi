from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import load_settings  # noqa: E402
from kvizi.question_report import build_report, find_duplicate_ids, load_bound_topic_keys  # noqa: E402
from kvizi.questions import QuestionValidationError, load_questions  # noqa: E402


def main() -> None:
    settings = load_settings()
    duplicate_ids = find_duplicate_ids(settings.questions_path)
    try:
        bank = load_questions(settings.questions_path)
    except QuestionValidationError as exc:
        print(f"Questions ERROR: {exc}", file=sys.stderr)
        if duplicate_ids:
            print(f"Duplicate ids: {', '.join(duplicate_ids)}", file=sys.stderr)
        raise SystemExit(1) from exc

    bound_topics = load_bound_topic_keys(settings.database_path)
    lines, warnings = build_report(bank, duplicate_ids, bound_topics)
    for line in lines:
        print(line)
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")
        print(f"Warnings total: {len(warnings)}")


if __name__ == "__main__":
    main()
