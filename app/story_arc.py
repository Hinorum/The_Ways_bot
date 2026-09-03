"""Сквозная арка месяца: скелет истории на весь забег (день 1 -> День Первого Лая).

Новая структура — 4 акта вместо 7 этапов:
  Акт 1 «Вход» (0-27%): стая попадает в лабиринт, понимает правила, встречает Крысу
  Акт 2 «Поиск» (27-60%): находит Сердце лабиринта, понимает Администратора
  Акт 3 «Кризис» (60-93%): лабиринт ломается, стая у двери к Сердцу
  Акт 4 «Финал» (93-100%): выбор, взвешивание Анубиса, исход

Всё детерминировано: один и тот же день при одних и тех же входных данных
даёт один и тот же текст — для промпта, для офлайн-сборки и для вечерних
фолбэков. Структурный токен ЭТАП=N стабилен и парсится офлайн-сборкой лора.
"""

from __future__ import annotations

import random

# ЭТАП структуры месяца. Границы в долях (run_day-1)/(total-1):
PRIMET_TO_STAGE = (1, 2, 3)  # на каких актах являются приметы Лая №1..№3

_ARC_STAGES: tuple[dict, ...] = (
    {
        "index": 0,
        "name": "Вход",
        "purpose": "стая попадает в лабиринт; знакомство с лицами, первые шаги, первые стены",
        "tone": "тихая героика начала пути, осторожное любопытство чужого коридора",
        "missions": (
            "Ветер принёс в лагерь обрывок чужой карты — края обгорели, подпись незнакомая.",
            "У двери стaya нашла пустую миску, расставленную строго по кругу: место готово под всех.",
            "Пиксель поймал лапой искру, которая не погасла, а повисла огоньком над тропой — и ждала.",
        ),
        "whisper": (
            "Вечером у костра стaya пересчитала саму себя — впервые за долгое время счёт сошёлся.",
            "До темноты коридоры гудели вежливо, как собеседники, которые ещё не решили, о чём говорить.",
            "Над лагерем повисла чужая искра Пикселя и не гасла до звезды.",
        ),
        "teaser": (
            "Счёт сложился, но лабиринт ещё держит паузу — как держит дыхание, прежде чем назвать имя.",
            "Кость брошена, и даже двери притихли: кто-то ждёт, чем стaya подтвердит свой первый шаг.",
        ),
        "guest": "местный бродячий пёс со старой ошейниковой биркой, который показывает тропы и уходит без прощания",
    },
    {
        "index": 1,
        "name": "Поиск",
        "purpose": "стaya ищет Сердце лабиринта; понимает, кто такой Администратор и зачем он считает",
        "tone": "детективная тишина поиска с одной тёплой деталью стаи в каждой главе",
        "missions": (
            "Дождь принёс в лагерь карту без подписи: на ней отмечена одна точка — туда стаю и тянет.",
            "Вектор нашёл след, который ведёт по кругу и возвращается туда же: лабиринт закольцован вокруг стаи.",
            "Безымянная легла и смотрит на дверь, которую никто не замечал: она стоит между двумя другими.",
        ),
        "whisper": (
            "У костра Баркод занёс лапу над картой и не опустил: след, найденный утром, смотрел на него.",
            "Вечер вышел длиннее: лабиринт ждал, что стaya дорисует карту до конца.",
            "Пиксель ловил искры, и одна из них пахла старым железом — запахом чужой подписи.",
        ),
        "teaser": (
            "Место голосования вскрыто, но тишина после неё — та, когда счёт сошёлся слишком ровно.",
            "Архив запечатал папку и вздохнул, как вздыхают, когда правда только начинается.",
        ),
        "guest": "счётчик из глухого туннеля — ему не нужен свет, только звук лап по каменным плитам",
    },
    {
        "index": 2,
        "name": "Кризис",
        "purpose": "лабиринт ломается; стaya у двери к Сердцу, Администратор выходит из тени",
        "tone": "назревающая тишина, мир на пределе, последняя тёплая деталь перед финалом",
        "missions": (
            "Одна из троп сама подстроилась под стаю, пока та шла: лабиринт начал выбирать за неё и тормозит только её шагом.",
            "Двери выстроились цепочкой в одну сторону — как коридор, который Администратор достроил до конца.",
            "Еретик оставил на стене знак-апостроф и рядом — пустой след, как будто переписал копию мира.",
        ),
        "whisper": (
            "Костра сегодня не разжигали: стае не нужно было тепло — только слышно друг друга в темноте.",
            "Двери больше не гудят — они молчат в такт, и тишина стала голосом.",
            "Безымянная весь вечер стояла мордой к тому месту, где завтра будет Лай, и не врала себе.",
        ),
        "teaser": (
            "Счёт последний раз подвёл черту, и за ней раздался ровный лай сигнала.",
            "Место голосования остыло, а в папке дня лежит уже не просто итог — предпоследняя строка мира.",
        ),
        "guest": "существо, которое лечит мир шёпотом и просит не говорить спасибо — оно уже почти закончило",
    },
    {
        "index": 3,
        "name": "Финал",
        "purpose": "День Первого Лая: дом, ловушка или зов — три прочтения одного звука",
        "tone": "предельно разреженная, почти торжественная тишина: выбор звучит громче слов",
        "missions": (),
        "whisper": (
            "Перед Лаем у костра сидела вся стая целиком — первый раз с первого дня.",
            "В последний мир лабиринта двери светились ровно, без дрожи: стены тоже ждали.",
        ),
        "teaser": (),
        "guest": None,
    },
)


# Приметы Лая №1..№3 — сквозная линия месяца: по одной являются на актах
# Вход / Поиск / Кризис и нанизывают дни на общий миф.
_HOWL_SIGNS: tuple[tuple[str, ...], ...] = (
    (
        "Лай стал опережать утро: собаки слышат его за час до рассвета, хотя раньше звук шёл после.",
        "Слух у стаи обострился так, что в гудении коридора нет-нет да проступит ровный, как канон, Лай.",
        "Безымянная стала прикладывать ухо к земле по утрам — она слышит Лай даже под мокрой тропой.",
    ),
    (
        "Вода в мисках повторяет счёт стаи: по одной капле на каждый шаг, ровно в такт дыханию.",
        "Тени у карт ложатся под одним углом, будто рассчитывая — на кого из стаи упадёт выбор.",
        "Архив хранит одну и ту же страницу инвентаризации, и каждый раз счёт на ней свежий.",
    ),
    (
        "Небо над лабиринтом складывается в силуэт одной огромной собаки, которая ждёт.",
        "Стены наполовину перестали быть стенами: между коридорами проступает один общий запах — тёплого двора.",
        "Тишина перед Лаем обрела голос: негромкий, ровный, совсем близко — как сердце под землёй.",
    ),
)


# Секреты арки: по одному на акт-веху (примета №N). Вслед-шепот Ведущему,
# не противоречит канону DM_SYSTEM_PROMPT — лишь углубляет его. Число — номер
# приметы Лая (акты 1/2/3), с которой открывается секрет.
_ARC_SECRETS: dict[int, str] = {
    1: "в журнале архива есть страница, на которой стаи нет целиком: одной собаки в счёте не было с самого начала мира",
    2: "Администратор чинит не мир — он исправляет счёт, в котором когда-то ошибся сам, ровно на одну собаку",
    3: "Лайнер продал всю память, но его радио помнит всё; в ночь перед Лаем оно оживает первым, и голос у него — не Лайнера",
}


def arc_secret(milestone: int) -> str | None:
    return _ARC_SECRETS.get(milestone)


# Англ. сцены миссий для cover_prompt офлайн-обложки (мини-связка арта с аркой).
# Ключ — ТОЧНЫЙ текст миссии (детерминирован), значение — компактная визуальная
# сцена. Никакого LLM-перевода: переводы зафиксированы явно, в том же порядке,
# что и миссии. Читается как «кадр» дня месяца, а не случайная колода.
_ARC_MISSION_SCENES: dict[str, str] = {
    # Акт 0 «Вход»
    "Ветер принёс в лагерь обрывок чужой карты — края обгорели, подпись незнакомая.":
        "a burnt scrap of a stranger's map carried into camp by the wind, charred edges, unfamiliar signature",
    "У двери стaya нашла пустую миску, расставленную строго по кругу: место готово под всех.":
        "an empty bowl set in a perfect circle by the door, the place ready for the whole pack",
    "Пиксель поймал лапой искру, которая не погасла, а повисла огоньком над тропой — и ждала.":
        "a tiny spark caught in a dog's paw, not fading, hanging as a glowing ember above the path, waiting",
    # Акт 1 «Поиск»
    "Дождь принёс в лагерь карту без подписи: на ней отмечена одна точка — туда стаю и тянет.":
        "rain bringing a nameless map to camp with a single marked point that pulls the pack toward it",
    "Вектор нашёл след, который ведёт по кругу и возвращается туда же: лабиринт закольцован вокруг стаи.":
        "a dog tracing a scent that loops back to its start, the labyrinth circling around the pack",
    "Безымянная легла и смотрит на дверь, которую никто не замечал: она стоит между двумя другими.":
        "the Nameless lying still, watching a door nobody notices, standing between two others",
    # Акт 2 «Кризис»
    "Одна из троп сама подстроилась под стаю, пока та шла: лабиринт начал выбирать за неё и тормозит только её шагом.":
        "a trail adjusting itself to the pack's steps, the labyrinth starting to choose on its own, stalling with their stride",
    "Двери выстроились цепочкой в одну сторону — как коридор, который Администратор достроил до конца.":
        "doors lining up in a single chain, a corridor the Administrator built all the way to the end",
    "Еретик оставил на стене знак-апостроф и рядом — пустой след, как будто переписал копию мира.":
        "a heretic's apostrophe sign left on a wall beside an empty paw print, as if rewriting a copy of the world",
}


def mission_scene(mission_text: str | None) -> str:
    """Визуальная сцена миссии для cover_prompt ('' — если миссии нет)."""
    return _ARC_MISSION_SCENES.get(mission_text or "", "")


def _frac(run_day: int, total: int) -> float:
    if total <= 1:
        return 1.0
    return (max(1, run_day) - 1) / (total - 1)


# Названия карт дня по акту арки: описания/последствия берутся из общих
# архетип-пулов лора (риск/забота/хитрость), но ЛИЦО дня — из пула акта,
# чтобы тройка выбора читалась как часть текущего акта месяца, а не общая
# колода на все дни. По два варианта на акт — среди одноактных дней есть
# чем разнообразить выбор, а дедуп по свежим названиям всё равно работает.
_ARC_CARD_TITLES: dict[int, dict[str, tuple[str, ...]]] = {
    0: {
        "risk": ("Шаг в чужой гул", "Дверь, которая гудит"),
        "care": ("Миска на всех", "Спать у тёплого порога"),
        "cunning": ("След под чужим именем", "Тихий обход коридора"),
    },
    1: {
        "risk": ("Дойти до точки на карте", "Идти на чужой запах"),
        "care": ("Сжечь ложную тропу", "Найти того, кто потерял"),
        "cunning": ("Прочесть карту задом наперёд", "Заменить подпись на свою"),
    },
    2: {
        "risk": ("Коридор до конца", "Оставить след в тишине"),
        "care": ("Сдержать стаю от разбега", "Светить одной искрой на всех"),
        "cunning": ("Передать весть шёпотом", "Провести стаю в обход"),
    },
    3: {
        "risk": ("Дом за Лай", "Не отдать зов чужим"),
        "care": ("Первой услышать — первой встать", "Держать круг до конца"),
        "cunning": ("Поймать Лай в миски", "Разделить три прочтения зова"),
    },
}


def arc_card_titles(stage_idx: int, tag: str) -> tuple[str, ...]:
    """Названия карт акта по архетипу (риск/забота/хитрость)."""
    return _ARC_CARD_TITLES.get(stage_idx, {}).get(tag, ())


def arc_stage_index(run_day: int, total: int) -> int:
    """Индекс акта (0..3) для дня забега."""
    f = _frac(run_day, total)
    for stage in _ARC_STAGES:
        low, high = _STAGE_SPAN_BY_INDEX[stage["index"]]
        if low <= f < high:
            return stage["index"]
    return len(_ARC_STAGES) - 1


# Доли границ актов: внутренние, чтобы не зависеть от порядка словаря.
_STAGE_SPAN_BY_INDEX: dict[int, tuple[float, float]] = {
    0: (0.0, 0.27),
    1: (0.27, 0.60),
    2: (0.60, 0.93),
    3: (0.93, 1.01),
}


def arc_stage(run_day: int, total: int) -> dict:
    idx = arc_stage_index(run_day, total)
    return next(s for s in _ARC_STAGES if s["index"] == idx)


def _rng(seed_key: str, run_day: int) -> random.Random:
    return random.Random(f"arc:{seed_key}:{run_day}")


def mission_for(run_day: int, total: int, run_key: str = "") -> str:
    """Миссия дня: одна повествовательная зацепка для главы (детерминирована)."""
    stage = arc_stage(run_day, total)
    missions = stage["missions"]
    if not missions:
        return ""
    rng = _rng(run_key or "month", run_day)
    return missions[rng.randrange(len(missions))]


def whisper_pool(run_day: int, total: int) -> tuple[str, ...]:
    """Офлайн-пул вечерних реплик для акта (детерминирован)."""
    return arc_stage(run_day, total)["whisper"]


def whisper_pool_for_stage(stage_idx: int) -> tuple[str, ...]:
    """Пул вечерних реплик по индексу акта (для вызовов без total)."""
    return next(s for s in _ARC_STAGES if s["index"] == stage_idx)["whisper"]


def teaser_pool(run_day: int, total: int) -> tuple[str, ...]:
    """Офлайн-пул тизеров подсчёта для акта (детерминирован)."""
    return arc_stage(run_day, total)["teaser"]


def sign_for(run_day: int, total: int) -> tuple[str, str] | None:
    """Примета Лая на день-веху: (номер, текст) или None.

    Примета №1 является в первом дне акта «Поиск», №2 — «Кризис»,
    №3 — «Финал». Так месяц читается как один миф, а не набор дней.
    """
    idx = arc_stage_index(run_day, total)
    if idx not in PRIMET_TO_STAGE:
        return None
    num = PRIMET_TO_STAGE.index(idx) + 1
    pool = _HOWL_SIGNS[num - 1]
    rng = _rng("signs", run_day)
    return f"№{num}", pool[rng.randrange(len(pool))]


def _stage_label(run_day: int, total: int) -> dict:
    stage = arc_stage(run_day, total)
    sign = sign_for(run_day, total)
    return stage, sign


def arc_block(
    run_day: int,
    total: int,
    run_key: str = "",
    previous_season_summary: str | None = None,
) -> str:
    """Блок арки для season_block (идёт в промпт главы и в офлайн-сборку).

    Структурный токен ЭТАП=N стабилен и парсится offline-сборкой лора;
    остальное — повествование для Ведущего. Детерминирован по дню/забегу.
    """
    if total <= 1:
        return ""
    stage, sign = _stage_label(run_day, total)
    mission = mission_for(run_day, total, run_key)
    lines = [
        f"АРКА МЕСЯЦА | ЭТАП={stage['index']} | {stage['name']} | день {run_day} из {total}.",
        f"Сквозная линия месяца: три приметы Лая проступают сквозь стены лабиринта; этот акт — "
        f"{stage['purpose']}.",
    ]
    if run_key and run_day <= 2 and previous_season_summary:
        lines.append(
            f"Память прошлого месяца (продолжение мира): {previous_season_summary}."
        )
    if sign is not None:
        lines.append(f"ПРИМЕТА ЛАЯ {sign[0]}: {sign[1]}")
        secret = arc_secret(int(sign[0].lstrip("№")))
        if secret:
            lines.append(
                f"СЕКРЕТ АРКИ (Ведущему, одной фразой, вслух как тайну не называть): {secret}."
            )
    if mission:
        lines.append(f"Миссия дня: {mission}")
    lines.append(f"Тон: {stage['tone']}.")
    if stage.get("guest"):
        lines.append(
            f"Лицо арки (ввести на усмотрение Ведущего, одной сценой, без канонизации): "
            f"{stage['guest']}."
        )
    return "\n".join(lines)


def arc_details_from_block(season_block: str | None) -> dict:
    """Разбирает токены арки из season_block для офлайн-сборки лора.

    Возвращает {"stage": int, "mission": str} (или пустой dict, если арки нет).
    """
    if not season_block:
        return {}
    stage_idx: int | None = None
    mission = ""
    for line in season_block.splitlines():
        if line.startswith("АРКА МЕСЯЦА | ЭТАП="):
            try:
                stage_idx = int(line.split("ЭТАП=", 1)[1].split("|", 1)[0].strip())
            except (ValueError, IndexError):
                stage_idx = None
        elif line.startswith("Миссия дня: "):
            mission = line[len("Миссия дня: ") :].strip()
    if stage_idx is None and not mission:
        return {}
    return {"stage": stage_idx, "mission": mission}


async def load_season_arc_from_db(session, season: int) -> list[dict] | None:
    """Загружает арку сезона из БД. Возвращает список этапов или None."""
    from sqlalchemy import select as sa_select
    from app.models import SeasonArc

    q = sa_select(SeasonArc).where(SeasonArc.season == season).order_by(SeasonArc.stage_index)
    result = await session.execute(q)
    rows = result.scalars().all()

    if not rows:
        return None

    stages = []
    for row in rows:
        import json
        stages.append({
            "index": row.stage_index,
            "name": row.name,
            "purpose": row.purpose,
            "tone": row.tone,
            "missions": tuple(json.loads(row.missions_json)) if row.missions_json else (),
            "whisper": tuple(json.loads(row.whisper_json)) if row.whisper_json else (),
            "teaser": tuple(json.loads(row.teaser_json)) if row.teaser_json else (),
            "guest": row.guest or None,
        })
    return stages


async def seed_season_arcs(session, llm_caller=None, season: int = 1) -> int:
    """Заполняет таблицу season_arcs начальными данными.

    Если передан llm_caller — генерирует через LLM.
    Иначе — использует хардкод как фолбэк.
    """
    from sqlalchemy import select as sa_select, func as sa_func
    from app.models import SeasonArc
    import json

    inserted = 0
    for stage in _ARC_STAGES:
        # Проверяем существование
        q = (
            sa_select(sa_func.count())
            .select_from(SeasonArc)
            .where(SeasonArc.season == season, SeasonArc.stage_index == stage["index"])
        )
        result = await session.execute(q)
        exists = result.scalar() > 0
        if exists:
            continue

        # Если есть LLM — генерируем через AI
        name = stage["name"]
        purpose = stage["purpose"]
        tone = stage.get("tone", "")
        missions = list(stage.get("missions", ()))
        whisper = list(stage.get("whisper", ()))
        teaser = list(stage.get("teaser", ()))
        guest = stage.get("guest", "")

        if llm_caller:
            try:
                ai_stage = await _generate_season_stage_via_llm(stage["index"], season, llm_caller)
                if ai_stage:
                    name = ai_stage["name"]
                    purpose = ai_stage["purpose"]
                    tone = ai_stage.get("tone", tone)
                    missions = ai_stage.get("missions", missions)
                    whisper = ai_stage.get("whisper", whisper)
                    teaser = ai_stage.get("teaser", teaser)
                    guest = ai_stage.get("guest", guest)
            except Exception:
                pass  # Используем фолбэк

        row = SeasonArc(
            season=season,
            stage_index=stage["index"],
            name=name,
            purpose=purpose,
            tone=tone,
            missions_json=json.dumps(missions, ensure_ascii=False),
            whisper_json=json.dumps(whisper, ensure_ascii=False),
            teaser_json=json.dumps(teaser, ensure_ascii=False),
            guest=guest,
        )
        session.add(row)
        inserted += 1

    await session.commit()
    return inserted


async def _generate_season_stage_via_llm(stage_index: int, season: int, llm_caller) -> dict | None:
    """Генерирует этап сезона через LLM."""

    stage_names = {0: "Вход", 1: "Поиск", 2: "Кризис", 3: "Финал"}
    stage_name = stage_names.get(stage_index, f"Этап {stage_index}")

    prompt = (
        f"Создай этап сюжетной арки для текстовой RPG в мире постапокалиптического лабиринта.\n\n"
        f"Сезон: {season}\n"
        f"Этап: {stage_name} (индекс {stage_index}/3)\n\n"
        f"Контекст: Стая из 5 собак проходит лабиринт. Каждый этап — 25% сезона.\n"
        f"Этапы: Вход (знакомство) → Поиск (расследование) → Кризис (ломка) → Финал (выбор)\n\n"
        f"Верни JSON:\n"
        f'{{"name": "Название этапа", '
        f'"purpose": "Цель этапа (1-2 предложения)", '
        f'"tone": "Тон (1 предложение)", '
        f'"missions": ["Миссия 1", "Миссия 2", "Миссия 3"], '
        f'"whisper": ["Шёпот 1", "Шёпот 2", "Шёпот 3"], '
        f'"teaser": ["Тизер 1", "Тизер 2"], '
        f'"guest": "Гость этапа (1 предложение)"}}\n\n'
        f"Стиль: тёмный, атмосферный, метафоричный."
    )

    messages = [{"role": "user", "content": prompt}]
    result = await llm_caller(messages, temperature=0.8, max_tokens=800, want_json=True)

    if not result:
        return None

    response = result[0] if isinstance(result, tuple) else result
    if isinstance(response, dict) and all(k in response for k in ("name", "purpose")):
        return {
            "name": response["name"][:80],
            "purpose": response["purpose"][:300],
            "tone": response.get("tone", "")[:200],
            "missions": [m[:150] for m in response.get("missions", [])],
            "whisper": [w[:150] for w in response.get("whisper", [])],
            "teaser": [t[:150] for t in response.get("teaser", [])],
            "guest": response.get("guest", "")[:200],
        }

    return None
