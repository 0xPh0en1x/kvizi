from __future__ import annotations

import random

from kvizi.routing import TopicRoute, TopicRouter


def test_router_excludes_previous_topic_when_alternative_exists() -> None:
    router = TopicRouter(random.Random(1))
    topics = [
        TopicRoute("network", 10, 100),
        TopicRoute("security", 11, 1),
    ]

    chosen = router.choose(topics, last_topic_key="network")

    assert chosen is not None
    assert chosen.topic_key == "security"


def test_router_returns_only_topic_even_if_it_was_previous() -> None:
    router = TopicRouter(random.Random(1))
    topics = [TopicRoute("network", 10, 1)]

    chosen = router.choose(topics, last_topic_key="network")

    assert chosen is not None
    assert chosen.topic_key == "network"
