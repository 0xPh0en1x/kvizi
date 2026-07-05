from __future__ import annotations

from pathlib import Path

import pytest

from kvizi.questions import QuestionValidationError, load_questions


HEADER = (
    "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
    "option_5,option_6,correct_option_ids,explanation,source\n"
)


def write_questions(path: Path, row: str) -> None:
    path.write_text(HEADER + row, encoding="utf-8")


def test_load_questions_converts_human_option_number_to_telegram_index(tmp_path: Path) -> None:
    path = tmp_path / "questions.csv"
    write_questions(path, "q1,network,normal,Question?,A,B,C,D,,,2,Because,Source\n")

    bank = load_questions(path)
    question = bank.get("q1")

    assert bank.count() == 1
    assert question is not None
    assert question.topic_key == "network"
    assert question.correct_option_id == 1


def test_load_questions_rejects_multiple_correct_options_for_quiz_mode(tmp_path: Path) -> None:
    path = tmp_path / "questions.csv"
    write_questions(path, "q1,network,normal,Question?,A,B,C,D,,,1;2,Because,Source\n")

    with pytest.raises(QuestionValidationError, match="exactly one"):
        load_questions(path)


def test_load_questions_accepts_course_like_difficulty_slug(tmp_path: Path) -> None:
    path = tmp_path / "questions.csv"
    write_questions(path, "q1,network,CCNA,Question?,A,B,C,D,,,1,Because,Source\n")

    bank = load_questions(path)

    assert bank.get("q1").difficulty == "ccna"  # type: ignore[union-attr]
