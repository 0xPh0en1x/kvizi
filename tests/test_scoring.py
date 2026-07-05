from __future__ import annotations

from kvizi.scoring import ScoreInput, calculate_score


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
