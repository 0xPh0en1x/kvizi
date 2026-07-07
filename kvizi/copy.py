from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from kvizi.scoring import CHALLENGE_ECONOMY, DIFFICULTY_BASE_POINTS, challenge_cost, challenge_reward


DEFAULT_DIFFICULTY_ORDER = ("easy", "normal", "hard")


def _choose(variants: Sequence[str], chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return chooser(variants) if chooser else variants[0]


def _render(
    variants: Sequence[str],
    chooser: Callable[[Sequence[str]], str] | None = None,
    **values: object,
) -> str:
    return _choose(variants, chooser).format(**values)


def ordered_difficulties(difficulties: Iterable[str]) -> list[str]:
    difficulty_set = {str(difficulty).strip().lower() for difficulty in difficulties if str(difficulty).strip()}
    standard = [difficulty for difficulty in DEFAULT_DIFFICULTY_ORDER if difficulty in difficulty_set]
    custom = sorted(difficulty_set - set(DEFAULT_DIFFICULTY_ORDER))
    return standard + custom


def rules_text(
    difficulty_points: dict[str, int] | None = None,
    challenge_economy: dict[str, dict[str, int]] | None = None,
) -> str:
    difficulty_points = difficulty_points or DIFFICULTY_BASE_POINTS
    challenge_economy = challenge_economy or CHALLENGE_ECONOMY
    difficulties = ordered_difficulties(difficulty_points)
    points_text = ", ".join(
        f"{difficulty} - {difficulty_points[difficulty]}"
        for difficulty in difficulties
    )
    challenge_text = ", ".join(
        f"{difficulty} {challenge_cost(difficulty, challenge_economy)}->{challenge_reward(difficulty, challenge_economy)}"
        for difficulty in difficulties
    )
    return (
        "Добро пожаловать в манеж Квизи!\n\n"
        f"Верный ответ приносит очки: {points_text}.\n"
        "Кнопки x2 и x3 повышают награду, но добавляют риск: ошибка на x2 снимает базу, "
        "ошибка на x3 снимает две базы. Счет ниже нуля не падает.\n"
        "Серии дают бонусы: +3 на третьем верном ответе подряд, +7 на пятом, +15 на десятом.\n"
        "Ставку надо нажать до ответа в опросе. После ответа аппарат уже щелкнул."
        "\n\n"
        "Табло: /top для общего рейтинга, /top <topic_key> для рейтинга сектора.\n"
        "\n"
        "Вызов: /kvizi_challenge <difficulty> внутри привязанного топика.\n"
        f"Вызовы: {challenge_text}.\n"
        "Если ошибся или не ответил до закрытия, стоимость вызова сгорает."
    )


RULES_TEXT = rules_text()


USER_HELP_TEXT = (
    "Квизи-справка для участников:\n"
    "\n"
    "Играть:\n"
    "/me - твой счёт, серия и статистика\n"
    "/top - общее табло сезона\n"
    "/top <topic_key> - табло сектора, например /top network\n"
    "/rules - правила очков, ставок и вызовов\n"
    "/kvizi_challenge <difficulty> - вызвать личный вопрос в текущем топике\n"
    "\n"
    "Ставки:\n"
    "Кнопки x2/x3 жми до ответа. Верно - множитель радуется. Ошибка - бухгалтерия тоже.\n"
    "\n"
    "Подсказка из-под прожектора: команды работают внутри этой группы, не в личке."
)


def config_text(
    difficulty_points: dict[str, int] | None = None,
    challenge_economy: dict[str, dict[str, int]] | None = None,
) -> str:
    difficulty_points = difficulty_points or DIFFICULTY_BASE_POINTS
    challenge_economy = challenge_economy or CHALLENGE_ECONOMY
    difficulties = ordered_difficulties(difficulty_points)
    lines = [
        "Конфиг Квизи:",
        "Очки за верный ответ:",
    ]
    lines.extend(f"- {difficulty}: {difficulty_points[difficulty]}" for difficulty in difficulties)
    lines.append("Вызовы:")
    lines.extend(
        (
            f"- {difficulty}: стоимость {challenge_cost(difficulty, challenge_economy)}, "
            f"награда +{challenge_reward(difficulty, challenge_economy)}"
        )
        for difficulty in difficulties
    )
    return "\n".join(lines)

ADMIN_HELP_TEXT = (
    "Админ-пульт Квизи:\n"
    "\n"
    "Игра:\n"
    "/kvizi_postnow [topic_key] - отправить вопрос сейчас\n"
    "/kvizi_close_here - закрыть активные вопросы в текущем топике\n"
    "/kvizi_daily - отправить итоги дня сюда\n"
    "/kvizi_season_reset - сбросить текущий сезон\n"
    "\n"
    "Топики и настройки:\n"
    "/kvizi_bind <topic_key> <weight> - привязать текущий топик\n"
    "/kvizi_topics - список привязанных топиков\n"
    "/kvizi_announce_here - назначить топик анонсов\n"
    "/kvizi_config - текущий баланс очков и вызовов\n"
    "\n"
    "Контент:\n"
    "/kvizi_questions_status - покрытие questions.csv\n"
    "/kvizi_questions_template [difficulty] - CSV-шаблон вопросов\n"
    "/kvizi_upload_questions [--check] - проверить или заменить questions.csv\n"
    "/kvizi_backups - список backup questions.csv\n"
    "/kvizi_restore_questions <n> - восстановить backup questions.csv\n"
    "/kvizi_reload - перечитать questions.csv\n"
    "\n"
    "Диагностика:\n"
    "/kvizi_help_admin - эта справка\n"
    "/kvizi_help - справка для участников\n"
    "/kvizi_prod_check - быстрый production-check\n"
    "/kvizi_status - подробный статус\n"
    "/kvizi_status_compact - короткий статус\n"
    "/kvizi_recent - последние вопросы и ответы\n"
    "/kvizi_errors - последние ошибки Telegram/cron\n"
    "/kvizi_review - ревизия качества вопросов\n"
    "/kvizi_voice_preview - пример текущего голоса Квизи\n"
    "/kvizi_export [--full] - выгрузить состояние JSON\n"
    "\n"
    "Cron endpoints:\n"
    "POST /cron/tick - плановый вопрос\n"
    "POST /cron/maintenance - закрыть истёкшие poll\n"
    "POST /cron/daily - автоматические итоги дня\n"
    "POST /cron/backup - JSON backup админам\n"
    "\n"
    "Локально:\n"
    "python scripts/local_cron.py tick\n"
    "python scripts/local_cron.py maintenance\n"
    "python scripts/local_cron.py daily\n"
    "python scripts/local_cron.py backup"
)


POLL_TITLE_TEMPLATES = (
    "Та-даа! Вопрос вышел на сцену: {question}",
    "Квизи спрашивает, сияя слишком официально: {question}",
    "Click-click! Маленький экзамен открыл люк: {question}",
    "Прожектор нашел кандидата. Как удобно: {question}",
    "Вопрос из автомата. Автомат уверяет, что он почти лицензирован: {question}",
    "Bzzzt! Проверка компетентности, завернутая в блестящую фольгу: {question}",
    "Пиксели построились в шеренгу и требуют ответа: {question}",
    "Мини-аттракцион без очереди и с подозрительно бодрой музыкой: {question}",
)

QUESTION_INTRO_TEMPLATES = (
    "Та-даа: {topic_key}. {difficulty}, база {base}.",
    "Та-даа! Сектор {topic_key}, вопрос уже улыбается. Сложность {difficulty}, база {base}. Это нормально. Наверное.",
    "Внимание, {topic_key}: прожектор вежливо упал на вас. {difficulty}, база {base}. Аплодисменты можно отложить.",
    "Квизи выкатывает вопрос в сектор {topic_key}. Сложность {difficulty}, база {base}. Ничего личного, просто кнопки шептались.",
    "{topic_key}: вопрос доставлен. {difficulty}, база {base}. Да, можно было спокойно жить еще пять минут, но меню решило иначе.",
    "Bzzzt! Сектор {topic_key}, цифровая сцена требует маленький номер. Сложность {difficulty}, база {base}.",
    "Сектор {topic_key}, держите блестящую карточку судьбы: {difficulty}, база {base}. Карточка почти не смотрит в ответ.",
    "Click-click, {topic_key}! Новый вопрос встал в центр манежа. Сложность {difficulty}, база {base}. Просьба не спорить с прожектором.",
    "Коротко: {topic_key}, вопрос. Длиннее: {difficulty}, база {base}, шестерёнки поют, Квизи делает вид, что всё под контролем.",
    "О, {topic_key}! Какое совпадение: вопрос как раз искал сектор с хорошей акустикой. {difficulty}, база {base}.",
    "Сектор {topic_key}, пожалуйста сохраняйте спокойствие. Или хотя бы вид. Сложность {difficulty}, база {base}.",
    "Квизи открывает люк в сектор {topic_key}: внутри вопрос, {difficulty}, база {base} и немного ERROR-confetti.",
)

BET_ACCEPTED_TEMPLATES = (
    "Ставка x{stake} принята. Шестерёнки риска уже улыбаются. Это рабочий звук.",
    "Click-click! x{stake} зафиксирован. Великолепная идея, если любить острые бухгалтерские углы.",
    "x{stake} на табло. Аппарат делает вид, что не ждал именно этого.",
    "Риск x{stake} включён. Квизи выдаёт вам маленькую ленту смелости и чек.",
    "Ставка x{stake} записана. Не моргайте: кнопки обожают свидетелей.",
    "Та-даа, x{stake}! Теперь ответ будет либо славой, либо обучающим моментом с вычетом.",
    "Рычаг x{stake} щёлкнул. Если что, это вы сами нажали. Квизи просто красиво подсветил.",
    "Прекрасно! x{stake} принят, конфетти разрешено, паника временно отменяется.",
    "Bzzzt! Множитель x{stake} проглочен системой. Система просит не задавать ей моральных вопросов.",
)

BET_REJECTED_TEMPLATES = (
    "Ой-ой! Рычаг риска ушёл в служебный коридор: {reason}",
    "Ставка не прошла. Великолепно, тайна уже началась: {reason}",
    "Квизи отклоняет номер с чрезмерной вежливостью: {reason}",
    "Аппарат ставок улыбнулся и закрыл окошко: {reason}",
    "Bzzzt. Множитель не приклеился к реальности: {reason}",
    "Нет-нет-нет, этот трюк не пролезает через пиксельную рамку: {reason}",
    "Ставочный механизм поднял табличку «почти»: {reason}",
)

SCORE_CHALLENGE_WIN_TEMPLATES = (
    "{name}: вызов пройден! +{delta}. Всего {points}. Та-даа, табло не успело спрятаться.",
    "{name}: challenge закрыт чисто. +{delta}. Всего {points}. Система недовольно признаёт красоту.",
    "{name}: личный номер удался. +{delta}. Всего {points}. Квизи хлопает, но очень профессионально.",
    "{name}: прожектор вызова стал зелёным. +{delta}. Всего {points}. Подозрительно компетентно.",
    "{name}: вызов пережил встречу с реальностью. Редкость! +{delta}. Всего {points}.",
    "{name}: соло-номер принят. +{delta}. Всего {points}. Пиксели делают вид, что не впечатлены.",
    "{name}: challenge прошёл. +{delta}. Всего {points}. Квизи записал это в папку «неожиданно приятно».",
)

SCORE_CHALLENGE_LOSS_TEMPLATES = (
    "{name}: вызов провален. {delta}. Всего {points}. Занавес упал очень вежливо.",
    "{name}: challenge споткнулся о маленький факт. {delta}. Всего {points}. Факт, конечно, не извинился.",
    "{name}: личный номер не прошёл проверку. {delta}. Всего {points}. Квизи сочувствует почти убедительно.",
    "{name}: прожектор вызова моргнул красным. {delta}. Всего {points}. Очень декоративная неудача.",
    "{name}: вызов посмотрел на ответ, на правила и ушёл в пиксельную паузу. {delta}. Всего {points}.",
    "{name}: соло-номер закрыт без фанфар. Зато с бухгалтерией. {delta}. Всего {points}.",
    "{name}: challenge не взлетел. Уверенность была, правильного ответа не хватило. {delta}. Всего {points}.",
)

SCORE_CORRECT_TEMPLATES = (
    "{name}: та-даа, верно. x{stake}, +{delta}{bonus}. Всего {points}.",
    "{name}: верно на x{stake}! +{delta}{bonus}. Всего {points}. Та-даа, компетентность обнаружена.",
    "{name}: точное попадание на x{stake}. +{delta}{bonus}. Всего {points}. Прожектор доволен. Странно, но мило.",
    "{name}: лампочка истины вспыхнула. x{stake}, +{delta}{bonus}. Всего {points}. Не трогайте лампочку.",
    "{name}: ответ принят. Сектор аплодирует мысленно. x{stake}, +{delta}{bonus}. Всего {points}.",
    "{name}: цифровая рулетка выдала «верно». x{stake}, +{delta}{bonus}. Всего {points}. Никто не паникует.",
    "{name}: верно. Квизи проверил два раза: доверие мило, SQL надёжнее. x{stake}, +{delta}{bonus}. Всего {points}.",
    "{name}: машина хотела драму, получила правильный ответ. x{stake}, +{delta}{bonus}. Всего {points}.",
    "{name}: правильно. Неприлично спокойно. x{stake}, +{delta}{bonus}. Всего {points}. Продолжаем сиять.",
    "{name}: ответ засчитан, табло довольно щёлкает. x{stake}, +{delta}{bonus}. Всего {points}.",
)

SCORE_WRONG_TEMPLATES = (
    "{name}: риск x{stake} щёлкнул не туда. {delta}. Всего {points}. Какая яркая учебная ситуация.",
    "{name}: x{stake} показал характер. {delta}. Всего {points}. Квизи не осуждает. Он протоколирует.",
    "{name}: ставка x{stake} ушла в дым-машину. {delta}. Всего {points}. Дым-машина довольна.",
    "{name}: аппарат риска моргнул красным. x{stake}, {delta}. Всего {points}. Очень выразительный пиксель.",
    "{name}: множитель сделал то, что риск обычно и делает: красиво забрал табличку. {delta}. Всего {points}.",
    "{name}: ответ промахнулся, множитель хлопнул дверцей. {delta}. Всего {points}. Click-click, урок доставлен.",
    "{name}: риск x{stake} был смелым. Не полезным, но смелым. {delta}. Всего {points}.",
    "{name}: аппарат сказал «интересная теория» и снял очки. {delta}. Всего {points}. Сервис улыбается.",
)

DAILY_TITLE_TEMPLATES = (
    "Итоги дня {date}:",
    "Занавес дня {date}: цифры выстроились и делают вид, что им не страшно:",
    "Вечернее табло {date}: сияет, щёлкает, слегка преувеличивает свою важность:",
    "Сводка манежа {date}: всё официально, даже если выглядит как фокус с проводами:",
    "Квизи закрывает смену {date}: улыбка закреплена, отчёт дрожит:",
    "Дневной протокол {date}: слегка помятый, но торжественно поданный:",
    "Отчёт за {date}. Да, Квизи умеет в бухгалтерию. Нет, это не делает происходящее нормальнее:",
    "Финальный звонок {date}: табло выжило, участники почти тоже:",
)

DAILY_TOP_HEADERS = (
    "Топ дня:",
    "Главные ловцы очков, временно допущенные к сиянию:",
    "Пьедестал дня. Осторожно, он гордится собой:",
    "Кто держал прожектор и не уронил его в меню:",
    "Кто сегодня почти не спорил с правильными ответами:",
    "Те, кого табло пока терпит с профессиональной улыбкой:",
)

DAILY_EMPTY_TOP_LINES = (
    "- сегодня табло ещё пустое. Оно улыбается, но это рабочая улыбка",
    "- никто не забрал очки из аппарата. Аппарат притворяется, что не обиделся",
    "- прожекторы ждали, но табло молчало. Очень концептуально",
    "- сегодня очки лежали на витрине и скучали в строгом порядке",
    "- участники сохранили загадочную дистанцию от результата. Уважительно, странно",
)

DAILY_CHALLENGE_HEADERS = (
    "Challenge-сцена:",
    "Личные номера, где кнопка «назад» была декоративной:",
    "Вызовы под прожектором, который всё видел и ничего не забыл:",
    "Соло-выступления, после которых бухгалтерия моргает:",
)

DAILY_RISK_HEADERS = (
    "Риск x2/x3:",
    "Ставки и рычаги, разложенные по аккуратным тревожным полочкам:",
    "Кто дёргал множители и называл это планом:",
    "Отдел рискованных решений:",
    "Множители, смелость и прочие причины для короткой паузы:",
)

SEASON_LEADER_TEMPLATES = (
    "Лидер сезона: {name} — {points}.",
    "Корона сезона сейчас у {name}: {points}. Она почти не искрит.",
    "На верхней лампе сезона {name} — {points}. Лампа утверждает, что всё честно.",
    "Сезонный трон пока занимает {name}: {points}. Просьба не раскачивать, он цифровой.",
    "Главный номер сезона на данный момент: {name} с {points}. Аппарат делает уважительный шум.",
)

NO_SEASON_LEADER_TEMPLATES = (
    "Лидер сезона: пока никто не вырвался вперед.",
    "Сезонная корона пока лежит без владельца и тихо заполняет форму ожидания.",
    "Главный пьедестал сезона ещё свободен. Он делает вид, что ему всё равно.",
    "Сезонный трон пустует. Очень драматично, очень неудобно для отчёта.",
    "Лидер сезона пока не обнаружен. Табло смотрит на всех с одинаковым подозрением.",
)

SEASON_LEADER_CHANGE_TEMPLATES = (
    "Та-даа! Табло сезона передумало: {new_name} выходит на первое место с {points}. {old_name}, просьба не спорить с прожектором.",
    "Смена лидера! {new_name} теперь наверху: {points}. {old_name} аккуратно сдвинут системой. Система улыбается.",
    "Click-click! Корона сезона переехала к {new_name}: {points}. {old_name}, это не падение, это декоративная перестановка.",
    "Квизи фиксирует новый верхний пиксель: {new_name} — {points}. Прошлый лидер {old_name} временно отправлен в режим драматичной паузы.",
    "Внимание, табло щёлкнуло слишком довольно: {new_name} стал лидером сезона с {points}. {old_name}, держим лицо. Оно полезно.",
)

STREAK_MILESTONE_TEMPLATES = (
    "{name} держит серию {streak}! +{bonus}. Табло нервно поправило невидимый галстук. Всего {points}.",
    "Та-даа! {name}: {streak} верных подряд, бонус +{bonus}. Квизи делает вид, что ожидал именно это. Всего {points}.",
    "Серия {streak} у {name}. +{bonus}. Пиксели выстроились в овацию, строго по инструкции. Всего {points}.",
    "Click-click! {name} собрал серию {streak}. Бонус +{bonus}; бухгалтерия вздохнула и согласилась. Всего {points}.",
    "Прожектор зафиксировал: {name}, серия {streak}, +{bonus}. Это уже похоже на привычку. Подозрительно полезную. Всего {points}.",
)


def poll_title(question: str, chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _render(POLL_TITLE_TEMPLATES, chooser, question=question)


def question_intro(
    topic_key: str,
    difficulty: str,
    base: int | None = None,
    chooser: Callable[[Sequence[str]], str] | None = None,
) -> str:
    base = DIFFICULTY_BASE_POINTS.get(difficulty, 10) if base is None else base
    return _render(
        QUESTION_INTRO_TEMPLATES,
        chooser,
        topic_key=topic_key,
        difficulty=difficulty,
        base=base,
    )


def question_announcement(
    topic_key: str,
    difficulty: str,
    link: str,
    base: int | None = None,
    chooser: Callable[[Sequence[str]], str] | None = None,
) -> str:
    return f"{question_intro(topic_key, difficulty, base, chooser)}\n{link}"


def bet_accepted(stake: int, chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _render(BET_ACCEPTED_TEMPLATES, chooser, stake=stake)


def bet_rejected(reason: str, chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _render(BET_REJECTED_TEMPLATES, chooser, reason=reason)


def score_event_text(
    *,
    name: object,
    is_challenge: bool,
    is_correct: bool,
    stake: int,
    delta: int,
    points: int,
    streak_bonus: int = 0,
    chooser: Callable[[Sequence[str]], str] | None = None,
) -> str:
    if is_challenge:
        variants = SCORE_CHALLENGE_WIN_TEMPLATES if is_correct else SCORE_CHALLENGE_LOSS_TEMPLATES
        return _render(variants, chooser, name=name, delta=delta, points=points)

    if is_correct:
        bonus = f", бонус серии +{streak_bonus}" if streak_bonus else ""
        return _render(
            SCORE_CORRECT_TEMPLATES,
            chooser,
            name=name,
            stake=stake,
            delta=delta,
            bonus=bonus,
            points=points,
        )

    return _render(SCORE_WRONG_TEMPLATES, chooser, name=name, stake=stake, delta=delta, points=points)


def daily_title(date: str, chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _render(DAILY_TITLE_TEMPLATES, chooser, date=date)


def daily_top_header(chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _choose(DAILY_TOP_HEADERS, chooser)


def daily_empty_top(chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _choose(DAILY_EMPTY_TOP_LINES, chooser)


def daily_challenge_header(chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _choose(DAILY_CHALLENGE_HEADERS, chooser)


def daily_risk_header(chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _choose(DAILY_RISK_HEADERS, chooser)


def season_leader_line(
    name: str,
    points: int,
    chooser: Callable[[Sequence[str]], str] | None = None,
) -> str:
    return _render(SEASON_LEADER_TEMPLATES, chooser, name=name, points=points)


def no_season_leader_line(chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _choose(NO_SEASON_LEADER_TEMPLATES, chooser)


def season_leader_change(
    new_name: str,
    old_name: str,
    points: int,
    chooser: Callable[[Sequence[str]], str] | None = None,
) -> str:
    return _render(
        SEASON_LEADER_CHANGE_TEMPLATES,
        chooser,
        new_name=new_name,
        old_name=old_name,
        points=points,
    )


def streak_milestone(
    name: str,
    streak: int,
    bonus: int,
    points: int,
    chooser: Callable[[Sequence[str]], str] | None = None,
) -> str:
    return _render(
        STREAK_MILESTONE_TEMPLATES,
        chooser,
        name=name,
        streak=streak,
        bonus=bonus,
        points=points,
    )


def no_questions_text(chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return _choose(
        (
            "Вопросов нет. Манеж пуст, прожекторы грустят.",
            "Вопросы закончились. Квизи слышит только гул ламп.",
            "Пока нечего спрашивать: карточная машина пуста.",
            "Вопросов нет. Это либо пауза, либо кто-то слишком хорошо спрятал CSV.",
            "Карточки закончились. Квизи делает вид, что так и было задумано.",
        ),
        chooser,
    )


def voice_preview_text(chooser: Callable[[Sequence[str]], str] | None = None) -> str:
    return "\n".join(
        [
            "Голосовой пример Квизи:",
            "",
            "Опрос:",
            poll_title("Что делает DNS?", chooser),
            "",
            "Анонс:",
            question_announcement(
                topic_key="network",
                difficulty="normal",
                base=10,
                link="https://t.me/c/123456789/42",
                chooser=chooser,
            ),
            "",
            "Ставки:",
            f"- {bet_accepted(3, chooser)}",
            f"- {bet_rejected('ответ уже принят', chooser)}",
            "",
            "Счёт:",
            "- "
            + score_event_text(
                name="@guest",
                is_challenge=False,
                is_correct=True,
                stake=2,
                delta=23,
                streak_bonus=3,
                points=48,
                chooser=chooser,
            ),
            "- "
            + score_event_text(
                name="@guest",
                is_challenge=False,
                is_correct=False,
                stake=3,
                delta=-20,
                points=28,
                chooser=chooser,
            ),
            "",
            "Итоги дня:",
            daily_title("07.07.2026 MSK", chooser),
            daily_top_header(chooser),
            season_leader_line("@guest", 99, chooser),
        ]
    )


def top_header(season: str) -> str:
    return f"Табло сезона {season}:"


def admin_only() -> str:
    return "Эта ручка только для администраторов Квизи."
