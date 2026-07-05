from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import load_settings  # noqa: E402
from kvizi.questions import load_questions  # noqa: E402


def main() -> None:
    settings = load_settings()
    bank = load_questions(settings.questions_path)
    topics = ", ".join(sorted(bank.topics())) or "none"
    print(f"Questions OK: {bank.count()}")
    print(f"Topics: {topics}")


if __name__ == "__main__":
    main()
