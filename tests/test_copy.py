from __future__ import annotations

from collections.abc import Sequence

from kvizi import copy


PERSONA_MARKERS = (
    "та-даа",
    "click-click",
    "bzzzt",
    "квизи",
    "аппарат",
    "систем",
    "рычаг",
    "меню",
    "люк",
    "бухгалтер",
    "пиксел",
    "прожектор",
    "манеж",
    "сцен",
    "шестер",
    "табло",
    "конфетти",
    "кноп",
    "аттракцион",
    "занавес",
    "корон",
    "трон",
    "номер",
    "фанфар",
    "автомат",
)


def _pick(index: int):
    def choose(options: Sequence[str]) -> str:
        return options[index]

    return choose


def test_question_intro_has_persona_variants() -> None:
    variants = {
        copy.question_intro("network", "normal", 10, _pick(index))
        for index in range(len(copy.QUESTION_INTRO_TEMPLATES))
    }

    assert len(variants) >= 5
    assert all("network" in variant for variant in variants)
    assert all("normal" in variant for variant in variants)
    assert all("10" in variant for variant in variants)
    lengths = [len(variant) for variant in variants]
    assert max(lengths) - min(lengths) >= 60


def test_persona_templates_stay_telegram_friendly_and_characterful() -> None:
    rendered = [
        *(template.format(question="Что делает DNS?") for template in copy.POLL_TITLE_TEMPLATES),
        *(
            template.format(topic_key="network", difficulty="normal", base=10)
            for template in copy.QUESTION_INTRO_TEMPLATES
        ),
        *(template.format(stake=3) for template in copy.BET_ACCEPTED_TEMPLATES),
        *(template.format(reason="ответ уже принят") for template in copy.BET_REJECTED_TEMPLATES),
        *(
            template.format(name="Ada", delta=25, points=48)
            for template in copy.SCORE_CHALLENGE_WIN_TEMPLATES
        ),
        *(
            template.format(name="Ada", delta=-10, points=20)
            for template in copy.SCORE_CHALLENGE_LOSS_TEMPLATES
        ),
        *(
            template.format(name="Ada", stake=3, delta=33, bonus=", бонус серии +3", points=48)
            for template in copy.SCORE_CORRECT_TEMPLATES
        ),
        *(
            template.format(name="Ada", stake=3, delta=-20, points=28)
            for template in copy.SCORE_WRONG_TEMPLATES
        ),
        *(template.format(date="07.07.2026 MSK") for template in copy.DAILY_TITLE_TEMPLATES),
        *copy.DAILY_TOP_HEADERS,
        *copy.DAILY_EMPTY_TOP_LINES,
        *copy.DAILY_CHALLENGE_HEADERS,
        *copy.DAILY_RISK_HEADERS,
        *(template.format(name="@ada", points=99) for template in copy.SEASON_LEADER_TEMPLATES),
        *copy.NO_SEASON_LEADER_TEMPLATES,
    ]

    short_enough = [line for line in rendered if len(line) <= 160]
    characterful = [
        line
        for line in rendered
        if any(marker in line.lower() for marker in PERSONA_MARKERS)
    ]

    assert len(short_enough) / len(rendered) >= 0.85
    assert len(characterful) / len(rendered) >= 0.65


def test_score_event_has_risk_variants() -> None:
    variants = {
        copy.score_event_text(
            name="Ada",
            is_challenge=False,
            is_correct=True,
            stake=3,
            delta=33,
            points=48,
            streak_bonus=3,
            chooser=_pick(index),
        )
        for index in range(len(copy.SCORE_CORRECT_TEMPLATES))
    }

    assert len(variants) >= 5
    assert all("Ada" in variant for variant in variants)
    assert all("48" in variant for variant in variants)
    lengths = [len(variant) for variant in variants]
    assert max(lengths) - min(lengths) >= 40
