from __future__ import annotations

from pathlib import Path

from kvizi.database import KviziRepository
from kvizi.question_report import build_report, find_duplicate_ids, load_bound_topic_keys
from kvizi.questions import load_questions


HEADER = (
    "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
    "option_5,option_6,correct_option_ids,explanation,source\n"
)


def test_validate_questions_report_includes_stats_and_binding_warnings(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.csv"
    questions_path.write_text(
        HEADER
        + "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n"
        + "q2,security,hard,Question?,A,B,C,D,,,2,Because,\n",
        encoding="utf-8",
    )
    repository = KviziRepository(tmp_path / "kvizi.sqlite3")
    repository.init_db()
    repository.bind_topic("network", 101, 1)
    repository.bind_topic("system", 102, 1)

    bank = load_questions(questions_path)
    bound_topics = load_bound_topic_keys(repository.database_path)
    lines, warnings = build_report(bank, bound_topics=bound_topics)

    assert "Questions OK: 2" in lines
    assert "Difficulties: hard=1, normal=1" in lines
    assert "- network: total=1 | normal=1 | missing standard: easy, hard" in lines
    assert "- security: total=1 | hard=1 | missing standard: easy, normal" in lines
    assert "Bindings: SQLite topics=network, system" in lines
    assert "CSV topics not bound in SQLite: security" in warnings
    assert "SQLite bound topics without questions: system" in warnings


def test_validate_questions_finds_duplicate_ids(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.csv"
    questions_path.write_text(
        HEADER
        + "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n"
        + "q1,network,hard,Question?,A,B,C,D,,,2,Because,\n"
        + "q2,network,easy,Question?,A,B,C,D,,,3,Because,\n",
        encoding="utf-8",
    )

    assert find_duplicate_ids(questions_path) == ["q1"]
