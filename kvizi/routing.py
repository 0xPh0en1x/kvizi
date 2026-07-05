from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class TopicRoute:
    topic_key: str
    message_thread_id: int
    weight: int
    title: str = ""


class TopicRouter:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def choose(self, topics: Sequence[TopicRoute], last_topic_key: str | None = None) -> TopicRoute | None:
        active = [topic for topic in topics if topic.weight > 0]
        if not active:
            return None

        candidates = active
        if last_topic_key and len(active) > 1:
            without_last = [topic for topic in active if topic.topic_key != last_topic_key]
            if without_last:
                candidates = without_last

        total_weight = sum(topic.weight for topic in candidates)
        pick = self._rng.uniform(0, total_weight)
        cursor = 0.0
        for topic in candidates:
            cursor += topic.weight
            if pick <= cursor:
                return topic
        return candidates[-1]
