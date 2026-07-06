from __future__ import annotations

import pytest

from kvizi.scoring import (
    ScoreInput,
    calculate_score,
    challenge_cost,
    challenge_reward,
    parse_challenge_economy,
    parse_difficulty_points,
)


def test_correct_answer_applies_stake_and_streak_milestone_bonus() -> None:
    result = calculate_score(
        ScoreInput(
            difficulty="normal",
            stake=3,
            is_correct=True,
            current_points=10,
            current_streak=2,
        )
    )

    assert result.delta == 33
    assert result.new_points == 43
    assert result.new_streak == 3
    assert result.streak_bonus == 3


def test_wrong_risk_answer_does_not_drop_below_zero() -> None:
    result = calculate_score(
        ScoreInput(
            difficulty="normal",
            stake=3,
            is_correct=False,
            current_points=10,
            current_streak=4,
        )
    )

    assert result.delta == -20
    assert result.new_points == 0
    assert result.new_streak == 0


def test_custom_difficulty_points_can_be_parsed_and_used() -> None:
    difficulty_points = parse_difficulty_points("ccna:20,linux_basics:12")

    result = calculate_score(
        ScoreInput(
            difficulty="ccna",
            stake=2,
            is_correct=True,
            current_points=0,
            current_streak=0,
        ),
        difficulty_points=difficulty_points,
    )

    assert difficulty_points["normal"] == 10
    assert difficulty_points["ccna"] == 20
    assert result.delta == 40


def test_custom_challenge_economy_can_be_parsed_and_used() -> None:
    economy = parse_challenge_economy("ccna:20:55")

    assert challenge_cost("ccna", economy) == 20
    assert challenge_reward("ccna", economy) == 55
    assert challenge_cost("unknown", economy) == 10
    assert challenge_reward("unknown", economy) == 25


def test_invalid_difficulty_points_raise_clear_error() -> None:
    with pytest.raises(ValueError, match="difficulty:points"):
        parse_difficulty_points("ccna")

    with pytest.raises(ValueError, match="greater than 0"):
        parse_difficulty_points("ccna:0")
