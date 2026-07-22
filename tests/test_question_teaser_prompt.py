from __future__ import annotations

import json

import pytest

from kvizi.ai import AIProviderError
from kvizi.prompts.question_teaser import (
    PROMPT_SKILL_NAME,
    build_question_teaser_messages,
    parse_question_teaser,
)


def test_prompt_skill_contains_contract_examples_and_only_safe_question_context() -> None:
    messages = build_question_teaser_messages(
        "network",
        "What resolves names?",
        variation=2,
    )

    assert PROMPT_SKILL_NAME == "question-teaser-v1"
    assert messages[0]["role"] == "system"
    assert "антипримеры" in messages[0]["content"]
    assert any(message["role"] == "assistant" for message in messages)
    request = json.loads(messages[-1]["content"])
    assert request == {
        "task": "question_teaser",
        "topic": "network",
        "question": "What resolves names?",
        "preview_variant": 2,
    }
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "SMTP" not in serialized
    assert "DHCP" not in serialized
    assert "question_link" not in serialized
    assert "base_points" not in serialized


def test_every_few_shot_example_obeys_its_own_anchor_contract() -> None:
    messages = build_question_teaser_messages("network", "What resolves names?")

    for index in range(1, len(messages) - 1, 2):
        example_input = json.loads(messages[index]["content"])
        example_output = messages[index + 1]["content"]
        parsed = parse_question_teaser(
            example_output,
            question_text=example_input["question"],
            max_chars=160,
        )
        assert parsed.anchor


def test_parse_question_teaser_accepts_exact_question_anchor() -> None:
    result = parse_question_teaser(
        json.dumps(
            {
                "teaser": "Доменное имя снова ищет адрес — сеть приготовила короткое собеседование.",
                "anchor": "доменное имя",
            },
            ensure_ascii=False,
        ),
        question_text="Какой механизм сопоставляет доменное имя с IP-адресом?",
        max_chars=160,
        forbidden_phrases=("DNS", "DHCP"),
    )

    assert result.teaser.startswith("Доменное имя")
    assert result.anchor == "доменное имя"


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    (
        ("не JSON", "valid JSON"),
        (json.dumps({"teaser": "Текст без второго поля."}), "teaser and anchor"),
        (
            json.dumps(
                {"teaser": "Сеть ждёт точного ответа.", "anchor": "несуществующая цитата"},
                ensure_ascii=False,
            ),
            "exact quote",
        ),
        (
            json.dumps(
                {"teaser": "Сеть ждёт точного ответа.", "anchor": "доменное имя"},
                ensure_ascii=False,
            ),
            "does not contain",
        ),
    ),
)
def test_parse_question_teaser_rejects_broken_contract(
    payload: str,
    error_fragment: str,
) -> None:
    with pytest.raises(AIProviderError, match=error_fragment) as error:
        parse_question_teaser(
            payload,
            question_text="Какой механизм сопоставляет доменное имя с IP-адресом?",
            max_chars=160,
        )

    assert error.value.kind == "invalid_output"
    assert error.value.retryable is False
