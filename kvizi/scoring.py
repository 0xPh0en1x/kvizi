from __future__ import annotations

import re
from dataclasses import dataclass


DIFFICULTY_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

DIFFICULTY_BASE_POINTS: dict[str, int] = {
    "easy": 5,
    "normal": 10,
    "hard": 15,
}

CHALLENGE_ECONOMY: dict[str, dict[str, int]] = {
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


def parse_difficulty_points(raw: str | None) -> dict[str, int]:
    points = dict(DIFFICULTY_BASE_POINTS)
    if not raw or not raw.strip():
        return points

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        difficulty, value = _split_exact(item, 2, "difficulty points", "difficulty:points")
        difficulty = _normalize_difficulty(difficulty)
        try:
            points_value = int(value)
        except ValueError as exc:
            raise ValueError(f"Invalid points for difficulty {difficulty}: {value}") from exc
        if points_value <= 0:
            raise ValueError(f"Points for difficulty {difficulty} must be greater than 0")
        points[difficulty] = points_value
    return points


def parse_challenge_economy(raw: str | None) -> dict[str, dict[str, int]]:
    economy = {difficulty: dict(values) for difficulty, values in CHALLENGE_ECONOMY.items()}
    if not raw or not raw.strip():
        return economy

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        difficulty, cost, reward = _split_exact(item, 3, "challenge economy", "difficulty:cost:reward")
        difficulty = _normalize_difficulty(difficulty)
        try:
            cost_value = int(cost)
            reward_value = int(reward)
        except ValueError as exc:
            raise ValueError(f"Invalid challenge economy for difficulty {difficulty}: {item}") from exc
        if cost_value <= 0 or reward_value <= 0:
            raise ValueError(f"Challenge cost and reward for difficulty {difficulty} must be greater than 0")
        economy[difficulty] = {"cost": cost_value, "reward": reward_value}
    return economy


def base_points(difficulty: str, difficulty_points: dict[str, int] | None = None) -> int:
    points = difficulty_points or DIFFICULTY_BASE_POINTS
    return points.get(difficulty, points.get("normal", DIFFICULTY_BASE_POINTS["normal"]))


def challenge_cost(difficulty: str, challenge_economy: dict[str, dict[str, int]] | None = None) -> int:
    economy = challenge_economy or CHALLENGE_ECONOMY
    return economy.get(difficulty, economy.get("normal", DEFAULT_CHALLENGE_ECONOMY))["cost"]


def challenge_reward(difficulty: str, challenge_economy: dict[str, dict[str, int]] | None = None) -> int:
    economy = challenge_economy or CHALLENGE_ECONOMY
    return economy.get(difficulty, economy.get("normal", DEFAULT_CHALLENGE_ECONOMY))["reward"]


def calculate_score(
    score_input: ScoreInput,
    difficulty_points: dict[str, int] | None = None,
) -> ScoreResult:
    stake = max(1, min(score_input.stake, 3))
    base = base_points(score_input.difficulty, difficulty_points)

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


def _normalize_difficulty(value: str) -> str:
    difficulty = value.strip().lower()
    if not DIFFICULTY_SLUG_PATTERN.match(difficulty):
        raise ValueError(f"Difficulty must be a slug like normal, hard, ccna: {value}")
    return difficulty


def _split_exact(item: str, parts_count: int, label: str, example: str) -> list[str]:
    parts = [part.strip() for part in item.split(":")]
    if len(parts) != parts_count or any(not part for part in parts):
        raise ValueError(f"Invalid {label} item {item!r}; expected {example}")
    return parts
