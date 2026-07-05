from __future__ import annotations

from dataclasses import dataclass


DIFFICULTY_BASE_POINTS = {
    "easy": 5,
    "normal": 10,
    "hard": 15,
}

CHALLENGE_ECONOMY = {
    "easy": {"cost": 5, "reward": 10},
    "normal": {"cost": 10, "reward": 25},
    "hard": {"cost": 15, "reward": 40},
}

DEFAULT_CHALLENGE_ECONOMY = CHALLENGE_ECONOMY["normal"]

STREAK_BONUSES = {
    3: 3,
    5: 7,
    10: 15,
}


@dataclass(frozen=True)
class ScoreInput:
    difficulty: str
    stake: int
    is_correct: bool
    current_points: int
    current_streak: int


@dataclass(frozen=True)
class ScoreResult:
    delta: int
    new_points: int
    new_streak: int
    streak_bonus: int


def base_points(difficulty: str) -> int:
    return DIFFICULTY_BASE_POINTS.get(difficulty, DIFFICULTY_BASE_POINTS["normal"])


def challenge_cost(difficulty: str) -> int:
    return CHALLENGE_ECONOMY.get(difficulty, DEFAULT_CHALLENGE_ECONOMY)["cost"]


def challenge_reward(difficulty: str) -> int:
    return CHALLENGE_ECONOMY.get(difficulty, DEFAULT_CHALLENGE_ECONOMY)["reward"]


def calculate_score(score_input: ScoreInput) -> ScoreResult:
    stake = max(1, min(score_input.stake, 3))
    base = base_points(score_input.difficulty)

    if score_input.is_correct:
        new_streak = score_input.current_streak + 1
        streak_bonus = STREAK_BONUSES.get(new_streak, 0)
        delta = base * stake + streak_bonus
    else:
        new_streak = 0
        streak_bonus = 0
        delta = 0 if stake == 1 else -base * (stake - 1)

    new_points = max(0, score_input.current_points + delta)
    return ScoreResult(
        delta=delta,
        new_points=new_points,
        new_streak=new_streak,
        streak_bonus=streak_bonus,
    )
