"""Сезоны мира: арка привязана к ЗАБЕГУ, а не к календарю.

Забег = период от полного сброса игры до следующего сброса. Якорь забега
(день месяца старта + месяц) живёт в watcher_state и переживает рестарты;
арка всегда имеет полную длину месяца старта, даже если сброс случился
24-го числа: акт 1 начинается со дня 1 забега, а День Первого Лая наступает,
когда забег дорастает до своей длины (длинные забеги циклятся каждые ~месяц).

Копилки недели/месяца остаются календарными — это экономика, не нарратив.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import random
from datetime import date, datetime, timezone

# Прочтения Первого Лая на финальном дне — ровно по одному на тег карты.
FINALE_CARDS = {
    "care": "дом",
    "risk": "ловушка",
    "cunning": "стать зовом",
}

_ACT_TONE = {
    1: (
        "Сезон юн: мир только расставляет приметы. Пусть странность будет "
        "одной и тихой — шорох, а не гром."
    ),
    2: (
        "Первый Лай слышится всё явственнее: приметы множатся, порталы "
        "путают ветки чаще обычного. Напряжение растёт медленно и неотвратимо."
    ),
    3: (
        "Кризис сезона: Лай почти не смолкает, порталы дрожат на грани. "
        "Мир сам идёт к развязке — стае остаётся решать, кем она войдёт в него."
    ),
}


def season_key(moment: datetime) -> str:
    """Ключ сезона «YYYY-MM» по UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m")


# ---------- Якорь забега ----------


def default_anchor(moment: datetime) -> dict:
    """Якорь «первый день прямо сейчас»: для новых инстансов до первого сброса."""
    return {"dom": day_of_month_of(moment), "key": season_key(moment)}


def day_of_month_of(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).day


def parse_anchor(raw: str | None) -> dict | None:
    """{"dom": 24, "key": "2026-08"} из watcher_state либо None."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        dom = int(data["dom"])
        key = str(data["key"])
        year, month = (int(part) for part in key.split("-"))
        if not 1 <= dom <= 31 or not 1 <= month <= 12:
            return None
        return {"dom": dom, "key": f"{year:04d}-{month:02d}"}
    except Exception:
        return None


_RUN_CACHE: dict | None = None


def set_run_anchor_cache(anchor: dict | None) -> None:
    global _RUN_CACHE
    _RUN_CACHE = anchor


def get_cached_anchor(moment: datetime | None = None) -> dict:
    """Кэшированный якорь для синхронного кода (посты дня); без него — «сейчас»."""
    if _RUN_CACHE is not None:
        return _RUN_CACHE
    return default_anchor(moment or datetime.now(timezone.utc))


def run_position(anchor: dict, moment: datetime) -> tuple[int, int]:
    """(день забега, длина забега).

    Забег длится месяц месяца-старта; длиннее месяца — цикл повторяется
    (нарративная арка перезапускается, счёты и экономика не трогаются).
    """
    year, month = (int(part) for part in anchor["key"].split("-"))
    dom = max(1, min(int(anchor["dom"]), 31))
    start = date(year, month, dom)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.astimezone(timezone.utc).date()
    raw = (today - start).days + 1
    total = calendar.monthrange(year, month)[1]
    if raw < 1:
        # Часовой пояс/гонка часов: считаем первым днём.
        raw = 1
    run_day = ((raw - 1) % total) + 1
    return run_day, total


def run_days_left(run_day: int, total: int) -> int:
    return max(0, total - run_day)


def is_run_finale(run_day: int, total: int) -> bool:
    return run_day >= total


# ---------- Акты ----------


def act_number(run_day: int) -> int:
    return 1 if run_day <= 7 else 2


def crisis_act(run_day: int, total: int) -> bool:
    """Последние семь дней забега — третий акт."""
    return total - run_day < 7


def act_line(run_day: int, total: int) -> str:
    act = 3 if crisis_act(run_day, total) else act_number(run_day)
    tone = _ACT_TONE[act]
    left = run_days_left(run_day, total)
    tail = (
        "Сегодняшний день — ДЕНЬ ПЕРВОГО ЛАЯ."
        if left == 0
        else f"До Дня Первого Лая осталось {left} дн."
    )
    return f"Сезон: акт {act}. {tone} {tail}"


def tag_balance_line(balance: dict[str, int]) -> str:
    parts = [f"{name}: {balance.get(tag, 0)}" for tag, name in
             (("risk", "риск"), ("care", "забота"), ("cunning", "хитрость"))]
    return "Характер стаи за сезон — " + ", ".join(parts) + "."


def finale_instruction(balance: dict[str, int]) -> str:
    """Блок финала: три прочтения Лая, исход зависит от характера стаи."""
    dominant = max(balance, key=lambda tag: balance.get(tag, 0)) if balance else "care"
    flavour = {
        "risk": "Стая пришла сюда с обнажёнными клыками — и мир отвечает тем же.",
        "care": "Стая несёт тепло мисок и вылизанных ран — и Лай пахнет домом.",
        "cunning": "Стая вынюхивала обходные тропы весь сезон — и теперь знает про Лай то, чего не знает никто.",
    }[dominant]
    cards_hint = ", ".join(
        f"«{readable}» (tag {tag})" for tag, readable in FINALE_CARDS.items()
    )
    return (
        "СЕГОДНЯ — ДЕНЬ ПЕРВОГО ЛАЯ, финал сезона. Стая стоит у источника зова. "
        f"Все три карты — три прочтения Лая: {cards_hint}. Ни одно не подаётся "
        "как правильное; каждое честно меняет мир. " + flavour + " "
        + tag_balance_line(balance)
        + " Эпилог дня закроет сезон одним вздохом — чем он отозвался."
    )


def opener_instruction(previous_finale_summary: str | None) -> str:
    """Первый день нового сезона: мир помнит, чем закрылся прошлый."""
    base = (
        "НОВЫЙ СЕЗОН: прошёл сезон, и он закрылся Днём Первого Лая. Счёты "
        "обнулены копилкой лидеров, но память сети жива — мир носит шрамы "
        "и подарки того решения. Не пересказывай финал, покажи его осадок: "
        "чем пахнет утро после Лая."
    )
    if previous_finale_summary:
        base += f" Осадок прошлого финала: {previous_finale_summary}"
    return base


def season_block(
    *,
    anchor: dict,
    moment: datetime,
    balance: dict[str, int] | None = None,
    previous_season_summary: str | None = None,
) -> str:
    """Готовый блок для промпта главы по якорю забега."""
    run_day, total = run_position(anchor, moment)
    if is_run_finale(run_day, total):
        return finale_instruction(balance or {})
    block = act_line(run_day, total)
    if run_day == 1:
        block += "\n" + opener_instruction(previous_season_summary)
    elif run_day == 2 and previous_season_summary:
        # Второй день ещё держит осадок финала, если день 1 собран до сброса.
        block += "\n" + opener_instruction(previous_season_summary)
    # Пролог забега: первые семь дней знакомят стаю с миром и лицами.
    from app.prologue import prologue_block

    pblock = prologue_block(run_day)
    if pblock:
        block += "\n" + pblock
    if midpoint_day(run_day, total):
        block += "\n" + _MIDPOINT_BLOCK
    return block


# ---------- План Хозяина Ошибки: сюжет-машина сезона ----------

VILLAIN_KEY = "villain_plot"
# Якорь забега: {"dom": день-месяца старта, "key": "YYYY-MM"} в watcher_state.
RUN_START_KEY = "run_season_anchor"

_VILLAIN_EVENTS: dict[int, tuple[str, ...]] = {
    0: (
        "В чужих папках архива стали появляться страницы, которых никто не приносил, — "
        "и все они описывают стаю с ошибкой в счёте.",
        "Портал перепутал двух собак местами и не заметил. Кто-то пересчитывал стаю — "
        "и сбился ровно на одну.",
        "Ночью миски наполнились сами, но еда была вчерашняя. Мир чинят не по погоде, а по памяти.",
        "В тумане у портала мелькнул силуэт без морды — и стая впервые обошла его молчанием.",
        "На ошейниках появились лишние метки: маленькие, аккуратные, явно чужие.",
    ),
    1: (
        "Хозяин Ошибки впервые показался целиком: тень над порталом, которая считала "
        "вслух — и каждый счёт был другим.",
        "Один из миров закрылся на сутки раньше срока. В журнале архива стоит подпись, "
        "похожая на помарку.",
        "Лайнер предложил стае «страховку от ошибок» — и сам вздрогнул от собственной фразы.",
        "Дверь, которую стая открыла неделю назад, открылась второй раз — наружу.",
    ),
    2: (
        "Хозяин Ошибки оставил послание в мисках: ровный ряд камешков и один кривой. "
        "Стая поняла приглашение, но не поняла куда.",
        "Архив объявил внеочередную инвентаризацию стаи. Всех пересчитывали трижды — "
        "и трижды счёт сходился только до четвёртой собаки.",
        "Первый Лай прозвучал днём и оборвался на середине. Так не лают ни дом, ни ловушка — "
        "так переспрашивают.",
        "Кто-то начал чинить мир заранее: тропы выпрямляются, глухие углы светятся. Стало удобно — и неуютно.",
    ),
    3: (
        "Порталы задрожали и выстроились цепочкой — все в одну сторону. Хозяин Ошибки больше "
        "не прячет план: он строит коридор к Первому Лаю.",
        "Сеть начала исправлять прошлое: старые дни в архиве переписываются под один счёт. "
        "Стая помнит иначе — пока.",
        "У развилок лежат таблички с готовыми решениями. Почерк вежливый. Ни одной ошибки.",
        "Хозяин Ошибки пересчитал стаю и не сбился. Впервые счёт сошёлся полностью — и это худшая примета.",
    ),
}

_VILLAIN_STAGE_TONE = {
    0: "он только пробует мир на прочность: приметы мелкие, почти бытовые",
    1: "его вмешательство стало явным: мир отвечает стае чужими решениями",
    2: "он обращается к стае напрямую: послания, инвентаризации, полушаги",
    3: "его ход сделан: план виден целиком, до финала сезона осталось дожить",
}


def villain_stage(run_day: int, total: int) -> int:
    """Ступень плана Хозяина Ошибки для дня забега (0..3)."""
    if total - run_day < 7:
        return 3
    if run_day >= max(8, total // 2):
        return 2
    if run_day >= 7:
        return 1
    return 0


def midpoint_day(run_day: int, total: int) -> bool:
    """Серединный поворот: первый день ступени 2 плана злодея.

    Структурное событие пустыни акта 2: день запечатан (как глухой), закон
    дня принудительно «медиана» — Середняк забирает развилку. Финалу не
    грозит: ступень 2 начинается минимум за семь дней до Лая.
    """
    return villain_stage(run_day, total) == 2 and run_day == max(8, total // 2)


_MIDPOINT_BLOCK = (
    "ПОВОРОТ СЕРЕДИНЫ: сегодня Хозяин Ошибки впервые действует открыто — "
    "мир на глазах «чинится» чужой рукой. Архив запечатал урну, а закон "
    "дня принадлежит Середняку. Покажи, как удобно стало тропам — и как "
    "неуютно от этого стало стае."
)


def villain_event(season_key_value: str, stage: int, salt: str = "") -> str:
    """Событие ступени плана. Соль делает перезапуски сезона разными:
    полный сброс стирает план, и новая арка начинается с других событий,
    хотя пул и тональность ступеней неизменны."""
    pool = _VILLAIN_EVENTS.get(stage) or _VILLAIN_EVENTS[0]
    digest = hashlib.sha256(f"villain:{season_key_value}:{stage}:{salt}".encode()).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    return rng.choice(pool)


def villain_prompt_block(events: list[str], stage: int) -> str | None:
    """Блок сюжета сезона для промпта главы. None — событий ещё нет."""
    if not events:
        return None
    tone = _VILLAIN_STAGE_TONE.get(stage, _VILLAIN_STAGE_TONE[0])
    lines = [
        "СЮЖЕТ СЕЗОНА — план Хозяина Ошибки (канон, уже свершившееся):"
    ]
    lines += [f"- {event}" for event in events[-3:]]
    lines.append(
        f"Текущая ступень: {tone}. Вплетай это фоном — одним касанием за главу "
        "(деталь, реплика, примета), не пересказывай список целиком."
    )
    return "\n".join(lines)
