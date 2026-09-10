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
from datetime import date, datetime, timedelta, timezone

from app.async_utils import unwrap_llm_json
from app.core.registry import RUN_START_KEY, VILLAIN_KEY  # noqa: F401 (ре-экспорт для app.rounds)

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


def season_lens_line(season: int) -> str:
    """Линза сезона: тот же лабиринт «из другой шкуры».

    Сезон 1 — шкура стаи (первый раз, свежий взгляд). Каждый следующий сезон
    мир пересказывается с чужой точки: администратор видит тот же записанный
    след как пересчёт, еретик — как переписанное правило. Дёшево меняет
    смысл финала без новой механики: «сезон 2» это не новые правила, а
    другой голос внутри тех же событий.
    """
    if season <= 1:
        return ""
    lens = {
        2: (
            "ЛИНЗА СЕЗОНА: это не продолжение истории стаи — это тот же лабиринт, "
            "прожитый заново по следам Администратора. Ведущий держит взгляд с его "
            "стороны: считает стаю вслух, чинит счёт с заднего хода, но не называет "
            "его врагом. Стая — то же, что была; изменился тот, кто её записывает."
        ),
        3: (
            "ЛИНЗА СЕЗОНА: это не продолжение истории стаи — это тот же лабиринт, "
            "переписанный Еретиком. Ведущий держит взгляд с его стороны: правила дня "
            "звучат как чужие переписанные строчки, стая повторяет путь, зная, что "
            "её след уже кем-то пройден. Он не антагонист — он режиссёр этого сезона."
        ),
    }
    default = (
        "ЛИНЗА СЕЗОНА: чем дольше лабиринт живёт, тем меньше в нём «новых» дней и "
        "тем больше пересказанных. Покажи тот же путь глазами того, кто уже был "
        "здесь, — сторону не убивай, просто поставь у выбора свой голос."
    )
    return lens.get(season, default)


# ---------- Якорь забега ----------


def default_anchor(moment: datetime) -> dict:
    """Якорь «первый день прямо сейчас»: для новых инстансов до первого сброса.

    Нрав стаи роллится случайно по всему диапазону осей — нейтраль возможна.
    В режиме замкнутого месячного цикла (settings.closed_month_loop) день старта
    форсируется на 1-е число месяца, чтобы арка была ровно один календарный
    месяц независимо от даты сброса.
    """
    order, moral = roll_axes()
    # Локальный импорт: избегаем кольцевой связи season <-> config на старте.
    from app.config import settings as _settings

    dom = 1 if _settings.closed_month_loop else day_of_month_of(moment)
    return {
        "dom": dom,
        "key": season_key(moment),
        "season": 1,
        "order_axis": order,
        "moral_axis": moral,
    }


def day_of_month_of(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).day


def parse_anchor(raw: str | None) -> dict | None:
    """{"dom","key"[,"order_axis","moral_axis"]} из watcher_state либо None."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        dom = int(data["dom"])
        key = str(data["key"])
        year, month = (int(part) for part in key.split("-"))
        if not 1 <= dom <= 31 or not 1 <= month <= 12:
            return None
        anchor: dict = {"dom": dom, "key": f"{year:04d}-{month:02d}"}
        if isinstance(data.get("season"), int) and data["season"] >= 1:
            anchor["season"] = data["season"]
        for axis in ("order_axis", "moral_axis"):
            if isinstance(data.get(axis), int):
                anchor[axis] = _clamp_axis(data[axis])
        return anchor
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


def _epoch_total(start_date: date, months: int) -> int:
    """Число дней в арке длиной `months` месяцев от `start_date` (включительно)."""
    months = max(1, int(months))
    end_month_index = (start_date.month - 1) + months
    end_year = start_date.year + end_month_index // 12
    end_month = end_month_index % 12 + 1
    end_day = min(start_date.day, calendar.monthrange(end_year, end_month)[1])
    end_date = date(end_year, end_month, end_day) - timedelta(days=1)
    return max(1, (end_date - start_date).days + 1)


def _run_position_full(anchor: dict, moment: datetime) -> tuple[int, int, int]:
    """(день_в_сезоне, длина_сезона, номер_сезона).

    Сезоны — последовательные эпохи: сезон 1 длится first_season_months,
    сезоны 2+ — run_length_months. Так первый сезон короткий и плотный
    («сильный»), а дальше арки длиннее, и у второго месяца появляется своя
    награда месячного лидерборда. Отсчёт идёт от стартовой даты якоря, поэтому
    функция чистая и не требует состояния между вызовами.
    """
    from app.config import settings as _settings

    year, month = (int(part) for part in anchor["key"].split("-"))
    dom = max(1, min(int(anchor["dom"]), 31))
    start = date(year, month, dom)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.astimezone(timezone.utc).date()
    abs_days = (today - start).days + 1

    season = 1
    epoch_start = start
    remaining = abs_days
    if remaining < 1:
        # Часовой пояс/гонка часов: считаем первым днём сезона 1.
        return 1, _epoch_total(epoch_start, _settings.first_season_months), 1
    guard = 0
    while True:
        months = (
            _settings.first_season_months if season == 1
            else _settings.run_length_months
        )
        total_k = _epoch_total(epoch_start, months)
        if remaining <= total_k:
            return remaining, total_k, season
        remaining -= total_k
        epoch_start = epoch_start + timedelta(days=total_k)
        season += 1
        guard += 1
        if guard > 200:
            # Страховка от зацикливания при кривых настройках длин.
            return max(1, remaining), max(1, total_k), season


def run_position(anchor: dict, moment: datetime) -> tuple[int, int]:
    """(день забега, длина забега). Длина арки зависит от номера сезона
    (см. _run_position_full): сезон 1 короче, сезоны 2+ длиннее.
    Копилки недели/месяца остаются календарными."""
    run_day, total, _season = _run_position_full(anchor, moment)
    return run_day, total


def current_season(anchor: dict, moment: datetime) -> int:
    """Номер текущего сезона (эпохи) для якоря и момента."""
    _run_day, _total, season = _run_position_full(anchor, moment)
    return season


def run_days_left(run_day: int, total: int) -> int:
    return max(0, total - run_day)


def is_run_finale(run_day: int, total: int) -> bool:
    return run_day >= total


# ---------- Акты ----------


def act_number(run_day: int) -> int:
    return 1 if run_day <= 7 else 2


def _crisis_window(total: int) -> int:
    """Длина кризисного акта масштабируется с длиной сезона: у месячного
    сезона — 7 дней, чтобы финал успевал нагнетаться и не сползал в плато."""
    return max(7, total // 5)


def crisis_act(run_day: int, total: int) -> bool:
    """Последние дни забега (окно _crisis_window) — третий акт."""
    return total - run_day < _crisis_window(total)


def act_line(run_day: int, total: int, season: int | None = None) -> str:
    act = 3 if crisis_act(run_day, total) else act_number(run_day)
    tone = _ACT_TONE[act]
    left = run_days_left(run_day, total)
    tail = (
        "Сегодняшний день — ДЕНЬ ПЕРВОГО ЛАЯ."
        if left == 0
        else f"До Дня Первого Лая осталось {left} дн."
    )
    season_tag = f"Сезон {season}. " if season else ""
    return f"{season_tag}Акт {act}. {tone} {tail}"


# ---------- Нрав стаи: две оси D&D (Порядок × Мораль) ----------

AXIS_MIN, AXIS_MAX = -2, 2
# Стартовая позиция полностью случайна по диапазону: нейтраль возможна,
# если так решил рандом — гарантированного смещения ни в одну сторону нет.
_AXIS_START_POOL = (-2, -1, 0, 1, 2)

# Дрейф от тега победившего пути. Оси развязаны: у каждой оси есть и «+», и «−»
# драйверы среди разных тегов, так что порядок и мораль не заперты одной связкой.
#   care    (добрые-законопослушные):  порядок +1, мораль +1
#   risk    (хаотики):                  порядок −1, мораль ±1 (знак от сида дня)
#   cunning (расчётливые-законники):    порядок +1, мораль −1
# Характер хаоса: беспорядок всегда (порядок −1), но его мораль непредсказуема —
# хаотик бывает и добрым, и злым (как в D&D: хаос ≠ зло). Знак морали выбирается
# детерминированно от сида дня победы (см. apply_alignment_drift), благодаря чему
# достижима и диагональ «добрые-хаотики» (−порядок, +мораль), которая иначе
# выпадала из покрытия.
from app.core.rules import ALIGNMENT_DRIFT as _ALIGNMENT_DRIFT


def roll_axes() -> tuple[int, int]:
    """Случайный ненулевой старт обеих осей."""
    return random.choice(_AXIS_START_POOL), random.choice(_AXIS_START_POOL)


def season_base_axes(anchor_key: str, season: int) -> tuple[int, int]:
    """Детерминированные стартовые оси для нового сезона.

    При смене эпохи оси перекатываются: визуальный и нарративный характер
    мира обновляются, а не тянутся из прошлого сезона. Дрейф от голосов
    стаи начинается заново с новой стартовой точки.
    """
    digest = hashlib.sha256(f"axes:{anchor_key}:{season}".encode()).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    return rng.choice(_AXIS_START_POOL), rng.choice(_AXIS_START_POOL)


def _clamp_axis(value: int) -> int:
    return max(AXIS_MIN, min(AXIS_MAX, int(value)))


def anchor_axes(anchor: dict) -> tuple[int, int]:
    """(порядок, мораль) из якоря; отсутствие полей = нейтраль."""
    return (
        _clamp_axis(anchor.get("order_axis", 0)),
        _clamp_axis(anchor.get("moral_axis", 0)),
    )


def apply_alignment_drift(
    anchor: dict, tag: str, seed: int | str | None = None
) -> tuple[int, int, bool]:
    """Двигает оси якоря по тегу победившего пути (мутация + возврат).

    Возвращает (порядок, мораль, изменилось_ли).

    seed — стабильный сид дня победы (обычно day_index). Нужен для тегов
    с неоднозначным направлением (хаос): знак морали выбирается
    детерминированно от сида, чтобы дрейф был воспроизводим, а не «настоящий
    рандом», который рвал бы аудит и реконструкцию дня.
    """
    moved = _ALIGNMENT_DRIFT.get(tag)
    if not moved:
        return anchor_axes(anchor), False
    rng = _rng(f"drift:{tag}:{seed}") if seed is not None else None
    changed = False
    for key, delta in moved.items():
        if callable(delta):
            delta = delta(rng)
        current = _clamp_axis(anchor.get(key, 0))
        fresh = _clamp_axis(current + delta)
        if fresh != current:
            anchor[key] = fresh
            changed = True
    order, moral = anchor_axes(anchor)
    return order, moral, changed


def alignment_label(order: int, moral: int) -> str:
    o_word = "законопослушная" if order > 0 else "хаотичная" if order < 0 else "нейтральная"
    m_word = "добрая" if moral > 0 else "злая" if moral < 0 else "нейтральная"
    if order == 0 and moral == 0:
        return "Нейтральная стая"
    return f"{o_word.capitalize()}-{m_word}"


def alignment_block(order: int, moral: int) -> str:
    """Блок характера для промпта главы: поведенческие директивы Ведущему."""
    label = alignment_label(order, moral)
    parts: list[str] = []
    if order > 0:
        parts.append("Правила и записи — опора стаи: дневник хранит расхождения, решения оформляются по протоколу, хаос раздражает.")
    elif order < 0:
        parts.append("Правила — препятствие: стая ищет лазы и обходы, ломает процедуры нарочно.")
    else:
        parts.append("К правилам стая равнодушна: соблюдает, когда удобно, игнорирует, когда нет.")
    if moral > 0:
        parts.append("Стая жертвует личной выгодой ради своих; чужая боль отзывается.")
    elif moral < 0:
        parts.append(
            "Выгода стаи превыше чужих ожиданий; обман и чёрный юмор уместны, "
            "но без смакования жестокости."
        )
    else:
        parts.append("Чужая боль и чужая выгода трогают стаю ровно настолько, насколько это выгодно.")
    body = " ".join(parts)
    return f"НРАВ СТАИ — {label}. {body} Держи подачу сцены, реплики и дилеммы в этом ключе."


def alignment_motifs(order: int, moral: int) -> list[str]:
    """Настроенческий мотив квадранта для визуальной библии дня."""
    table = {
        (1, 1): "warm orderly lantern glow over tidy rows",
        (1, -1): "cold seal-red bureaucratic light, stamped papers",
        (-1, 1): "wild gentle dawn haze, untamed but kind",
        (-1, -1): "ragged crimson glitch storm, crooked silhouettes",
    }
    key = (1 if order > 0 else -1 if order < 0 else 0,
           1 if moral > 0 else -1 if moral < 0 else 0)
    phrase = table.get(key, "grey even fog, balanced composition")
    return [phrase]


_ORDER_TINTS = (
    "Устав архива ложится на тропу, как размеченная дорожка: стая идёт по протоколу.",
    "Каждый поворот сверен с правилами — даже ветер сегодня ходит по регламенту.",
)
_CHAOS_TINTS = (
    "Правила здесь стареют быстрее собак — стая чует это шерстью и не жалует таблички.",
    "Тропа петляет назло разметке: хаос — родной язык этой стаи.",
)
_GOOD_TINTS = (
    "Доброта сегодняшних решений пахнет тёплой миской: стая делится, не считая.",
    "Стая оставляет лучший кусок тому, кто слабее — привычка сильнее голода.",
)
_EVIL_TINTS = (
    "Выгода прежде всего: стая смотрит на чужие миски без совести, но с юмором.",
    "Сегодня каждый решает, кого подставить под ошибку — и стая смеётся вполголоса.",
)


def _rng(seed: str) -> random.Random:
    """Детерминированный генератор на строке-сиде (зеркало lore._rng)."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def alignment_tints(order: int, moral: int, salt: str = "") -> list[str]:
    """Офлайн-тинты главы: по одному предложению на ненулевую ось."""
    rng = _rng(f"align:{salt}:{order}:{moral}")
    tints: list[str] = []
    if order != 0:
        pool = _CHAOS_TINTS if order < 0 else _ORDER_TINTS
        tints.append(pool[rng.randrange(len(pool))])
    if moral != 0:
        pool = _GOOD_TINTS if moral > 0 else _EVIL_TINTS
        tints.append(pool[rng.randrange(len(pool))])
    return tints


def alignment_finale_line(order: int, moral: int) -> str:
    label = alignment_label(order, moral).lower()
    return f"Нрав забега никуда не делся: стая вошла в Лай {label} — и Лай это запомнил."


_WEATHER_POOL = (
    "Сегодня тени идут против ветра — мир глючит красиво.",
    "Полдень наступил на час раньше; архив списал это на погоду.",
    "Все порталы сегодня одного оттенка. Так не бывает — и вот бывает.",
    "Дождь идёт только над картами выбора, не задевая миски.",
    "Эхо чужого дня прошло по стае вторым слоем: все на миг заговорили чужими голосами.",
)


def milestone_line(run_day: int, total: int) -> str | None:
    """Микропик акта 2: каждые 10 дней арки — аномалия-погода. None вне акта 2."""
    if run_day <= 7 or total - run_day < 7:
        return None
    if run_day % 10 != 0:
        return None
    rng = _rng(f"weather:{run_day}:{total}")
    # Weather pool: сначала из БД (AI), потом фолбэк
    from app.lore import get_weather_from_cache
    cached = get_weather_from_cache(1)
    pool = cached if cached and len(cached) >= 3 else _WEATHER_POOL
    return pool[rng.randrange(len(pool))]


def act_line_short(run_day: int, total: int, season: int | None = None) -> str:
    """Короткая строка акта для пульта/статусов без тонального абзаца."""
    act = 3 if crisis_act(run_day, total) else act_number(run_day)
    left = run_days_left(run_day, total)
    tail = "финал сегодня" if left == 0 else f"до Лая {left} дн."
    season_tag = f"Сезон {season} · " if season else ""
    return f"{season_tag}Акт {act} · {tail}"


def tag_balance_line(balance: dict[str, int]) -> str:
    parts = [f"{name}: {balance.get(tag, 0)}" for tag, name in
             (("risk", "риск"), ("care", "забота"), ("cunning", "хитрость"))]
    return "Характер стаи за сезон — " + ", ".join(parts) + "."


def finale_instruction(
    balance: dict[str, int],
    alignment: str | None = None,
    vow_count: int = 0,
    healed_memories: int = 0,
) -> str:
    """Блок финала: две честные цены, исход зависит от характера стаи.

    vow_count — сколько отложенных клятв стая не выкупила за сезон (стена
    у выхода); healed_memories — сколько личных памяти принято (целостность).
    Цена выхода — нарративная, не числовая: никаких штрафов и ресурсов.
    """
    dominant = max(balance, key=lambda tag: balance.get(tag, 0)) if balance else "care"
    flavour = {
        "risk": "Стая пришла сюда с обнажёнными клыками — и мир отвечает тем же.",
        "care": "Стая несёт тепло мисок и вылизанных ран — и Лай пахнет домом.",
        "cunning": "Стая вынюхивала обходные тропы весь сезон — и теперь знает про Лай то, чего не знает никто.",
    }[dominant]
    cards_hint = ", ".join(
        f"«{readable}» (tag {tag})" for tag, readable in FINALE_CARDS.items()
    )
    align_note = f" {alignment}" if alignment else ""
    vow_line = ""
    if vow_count:
        vow_line = (
            f" За сезон стая оставила у края тропы {vow_count} отложенных клятв: "
            "они сложились у выхода стеной, и теперь каждая — цена. Не считай их "
            "штрафом: мир не забирает, мир просит честно выбрать, какая клятва "
            f"будет выкуплена этим финалом {'— или что останется невыкупленным навсегда' if vow_count else ''}."
        )
    whole_line = ""
    if healed_memories:
        whole_line = (
            f" Память стаи приняла {healed_memories}/5 личных слоёв: дверь открывается "
            "цалой ровно для тех, кто цел внутри. Не наказывай за непринятые — "
            "покажи, что за ними остаётся свой честный выбор."
        )
    return (
        "СЕГОДНЯ — ДЕНЬ ПЕРВОГО ЛАЯ, финал сезона. Стая стоит у источника зова. "
        f"Все три карты — три прочтения Лая: {cards_hint}. Ни одно не подаётся "
        "как правильное; каждое честно меняет мир. " + flavour + " "
        "Это не выбор между добром и злом — это выбор между двумя честными ценами: "
        "за одну платит мир, за другую — стая. В этот финальный час смысл первого "
        "Лая изменяется тем, что выберет стая: то, что было зовом, становится ответом. "
        + vow_line
        + whole_line
        + " "
        + tag_balance_line(balance)
        + align_note
        + " Эпилог дня закроет сезон одним вздохом — чем он отозвался."
    )


def exodus_phase(run_day: int, total: int) -> int:
    """Фаза Исхода: 0 — не Исход; 1 — «выбор двери» (за 2 дня до Лая);
    2 — «кто несёт свет» (за день до Лая); 3 — День Первого Лая (финал).

    Исход — не отдельные правила, а рамка для промпта: мир не меняет чисел,
    он называет дни. Сезон закрывается трёхдневным дыханием вместо одиночного
    финала: сначала стая выбирает дверь, потом того, кто несёт свет, и только
    затем — Лая, который открывает или не открывает его.
    """
    if run_day >= total:
        return 3
    if run_day == total - 1:
        return 2
    if run_day == total - 2:
        return 1
    return 0


def exodus_instruction(
    phase: int,
    balance: dict[str, int],
    vow_count: int = 0,
    healed_memories: int = 0,
) -> str | None:
    """Блок предфинальных дней Исхода (фазы 1-2). phase 3 отдаёт финал обычным.

    Фаза 1 — «выбор двери»: каждая отложенная клятва — проём, который надо
    выбрать или оставить закрытым. Фаза 2 — «кто несёт свет»: стая решает,
    кого нести впереди, и это меняет тон самого Лая.
    """
    if phase == 1:
        base = (
            "ИСХОД, ДЕНЬ ПЕРВЫЙ — ВЫБОР ДВЕРИ. До Лая два дня. У стаи "
            "открывается не один выход, а столько, сколько было невыбранных путей: "
            "каждая отложенная клятва ждёт у своего проёма. Сегодня выбор не "
            "между «вперёд» и «назад», а между дверями. Пусть карты дня прозвучат "
            "как разные проёмы — за какой из них лабиринт закрывает счёт окончательно."
        )
        if vow_count:
            base += f" Отложенных клятв за сезон: {vow_count} — но сегодня стая выбирает не сколько заплатить, а какую дверь помнить."
        return base
    if phase == 2:
        base = (
            "ИСХОД, ДЕНЬ ВТОРОЙ — КТО НЕСЁТ СВЕТ. До Лая один день. Стая "
            "стоит перед спуском: кто идёт первым, кто несёт свет, кто остаётся "
            "замыкающим, чтобы закрыть за стаей дверь. В этом выборе нет проигравших "
            "собак — есть разные честные способы нести одну стаю. Завтра этот "
            "порядок станет голосом Лая: тот, кто впереди, услышит его первым."
        )
        if healed_memories:
            base += (
                f" Из пятерых память приняли {healed_memories}/5: примировавшие с собой "
                "несут свет иначе, чем те, кто ещё только вспоминает. Не суди их — покажи разницу тоном."
            )
        return base
    return None


def opener_instruction(previous_finale_summary: str | None, season: int = 1) -> str:
    """Первый день нового сезона: мир помнит, чем закрылся прошлый.

    Сезон 1: нет прошлого — opener не показывается (возвращается пустая строка).
    Сезон 2+: мир помнит осадок финала, короткое напоминание без повтора.
    """
    if season == 1 and not previous_finale_summary:
        return ""
    base = (
        "НОВЫЙ СЕЗОН: мир помнит осадок прошлого Лая. Счёты обнулены, "
        "но память сети жива — шрамы и подарки прошлого решения остались. "
        "Не пересказывай финал, покажи его осадок: чем пахнет утро после Лая."
    )
    if previous_finale_summary:
        base += f" Осадок прошлого финала: {previous_finale_summary}"
    return base


_CULMINATION_BLOCK = (
    "КУЛЬМИНАЦИЯ АРКИ: до Лая остались считанные дни, и оба плана вышли из тени. "
    "Администратор достраивает свой коридор в открытую, Еретик держит второй — "
    "и стая голосует уже не «за мир», а за то, чей коридор короче. Покажи мир "
    "на пределе натяжения: тропы звенят, порталы считают стаю вслух, каждый "
    "жест отзывается эхом в мисках. Финал не случится «потом» — он уже начался."
)


def season_banner(anchor: dict, moment: datetime) -> str | None:
    """Анонс нового сезона для поста дня. None, если сезон не сменился."""
    run_day, _total, season = _run_position_full(anchor, moment)
    if run_day == 1 and season > 1:
        return (
            f"НОВЫЙ СЕЗОН {season}. Прошлый закрылся Днём Первого Лая, и мир "
            "пересобрался заново: другая длина арки, другой счёт до финала. "
            "Стая помнит осадок прошлого Лая — но правила этого сезона пишутся сейчас."
        )
    return None


def season_block(
    *,
    anchor: dict,
    moment: datetime,
    balance: dict[str, int] | None = None,
    previous_season_summary: str | None = None,
    db_prologue_beats: dict | None = None,
    db_season_arc: list[dict] | None = None,
    vow_count: int = 0,
    healed_memories: int = 0,
) -> str:
    """Готовый блок для промпта главы по якорю забега.
    db_prologue_beats: AI-сгенерированные биты пролога из БД (опционально).
    db_season_arc: AI-сгенерированная арка сезона из БД (опционально).
    vow_count: отложенные клятвы сезона (стена у выхода).
    healed_memories: принятые личные памяти стаи (целостность).
    """
    run_day, total = run_position(anchor, moment)
    season = current_season(anchor, moment)
    order_axis, moral_axis = anchor_axes(anchor)
    lens = season_lens_line(season)
    phase = exodus_phase(run_day, total)
    if phase:
        if phase == 3:
            finale = finale_instruction(
                balance or {},
                alignment=alignment_label(order_axis, moral_axis),
                vow_count=vow_count,
                healed_memories=healed_memories,
            )
            return finale + ("\n" + lens if lens else "")
        exodus = exodus_instruction(
            phase, balance or {}, vow_count=vow_count, healed_memories=healed_memories
        )
        if exodus:
            block = exodus
            if lens:
                block += "\n" + lens
            align = alignment_block(order_axis, moral_axis)
            return f"{block}\n{align}"
    block = act_line(run_day, total, season)
    if lens:
        block += "\n" + lens
    if run_day == 1:
        opener = opener_instruction(previous_season_summary, season=season)
        if opener:
            block += "\n" + opener
    elif run_day == 2 and previous_season_summary:
        opener = opener_instruction(previous_season_summary, season=season)
        if opener:
            block += "\n" + opener
    banner = season_banner(anchor, moment)
    if banner:
        block += "\n" + banner
    # Пролог забега: первые семь дней знакомят стаю с миром и лицами.
    from app.prologue import prologue_block

    pblock = prologue_block(
        run_day, alignment_label=alignment_label(order_axis, moral_axis), season=season,
        db_beats=db_prologue_beats,
    )
    if pblock:
        block += "\n" + pblock
    # Нрав стаи — в каждую главу: подача сцены, реплики и дилеммы в ключе осей.
    block += "\n" + alignment_block(order_axis, moral_axis)
    if midpoint_day(run_day, total):
        block += "\n" + _MIDPOINT_BLOCK
    if recount_day(run_day, total):
        block += "\n" + _RECOUNT_BLOCK
    if crisis_act(run_day, total):
        block += "\n" + _CULMINATION_BLOCK
    return block


# ---------- План Хозяина Ошибки: сюжет-машина сезона ----------

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
        "Администратор впервые показался целиком: тень над порталом, которая считала "
        "вслух — и каждый счёт был другим.",
        "Один из миров закрылся на сутки раньше срока. В журнале архива стоит подпись, "
        "похожая на помарку.",
        "Лайнер предложил стае «страховку от ошибок» — и сам вздрогнул от собственной фразы.",
        "Дверь, которую стая открыла неделю назад, открылась второй раз — наружу.",
    ),
    2: (
        "Администратор оставил послание в мисках: ровный ряд камешков и один кривой. "
        "Стая поняла приглашение, но не поняла куда.",
        "Архив объявил внеочередную инвентаризацию стаи. Всех пересчитывали трижды — "
        "и трижды счёт сходился только до четвёртой собаки.",
        "Первый Лай прозвучал днём и оборвался на середине. Так не лают ни дом, ни ловушка — "
        "так переспрашивают.",
        "Кто-то начал чинить мир заранее: тропы выпрямляются, глухие углы светятся. Стало удобно — и неуютно.",
        "В чужих папках нашлись письма из старого мира: «здесь хотя бы миски полные». "
        "Дневник почему-то не стал их вычёркивать.",
    ),
    3: (
        "Порталы задрожали и выстроились цепочкой — все в одну сторону. Администратор больше "
        "не прячет план: он строит коридор к Первому Лаю.",
        "Сеть начала исправлять прошлое: старые дни в архиве переписываются под один счёт. "
        "Стая помнит иначе — пока.",
        "У развилок лежат таблички с готовыми решениями. Почерк вежливый. Ни одной ошибки.",
        "Администратор пересчитал стаю и не сбился. Впервые счёт сошёлся полностью — и это худшая примета.",
        "У развилок появилась вторая стопка табличек: почерк торопливый, с апострофом. "
        "Два плана теперь лежат рядом, и стая должна решить, чей коридор короче.",
        "На Базаре Лайнер выставил на прилавок своё немое радио: в ночи кризиса "
        "его ищет тот единственный, кто услышит в тишине ровный Лай. Пока никто не купил.",
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
    if total - run_day < _crisis_window(total):
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


def recount_day(run_day: int, total: int) -> bool:
    """День внеочередного пересчёта: четверть пути после середины (или середина
    длинного забега). Отдельное событие от midpoint — звук счёта из-за стен.

    Целое число после середины: run_day ≈ ceil(total * 3/4), но не позднее
    начала кризиса (ступень 3), чтобы финал остался свободным.
    """
    if crisis_act(run_day, total) or run_day <= 8:
        return False
    mark = max(9, (total * 3) // 4)
    return run_day == mark


_RECOUNT_BLOCK = (
    "ПЕРЕСЧЁТ. Сегодня второй звук, что стая слышала с первого дня, становится "
    "событием: за стенами проходит внеочередной пересчёт. Кто-то называет собак "
    "по одной, и счёт каждый раз сходится только до четвёртой — пятой в его "
    "списке нет, и он не может это исправить. Пересчёт — не наказание и не угроза: "
    "это попытка найти ту, которой, по его записям, никогда не было. Пусть звук "
    "идёт из-за стен, как дыхание гиганта; стая его слышит, но ответа у неё нет. "
    "Одна деталь этого дня должна остаться у стаи в памяти — и вернуться в финале."
)


_MIDPOINT_BLOCK = (
    "ПОВОРОТ СЕРЕДИНЫ. Сегодня мир перешагивает рубикон — это не просто "
    "очередной день, а само событие перелома. Администратор впервые действует "
    "открыто: тропы на глазах выпрямляются под чужой линейкой, дневник "
    "запечатан наглухо, а правило дня отдано Середняку — стая голосует вслепую. "
    "Покажи сам момент разлома: как удобно и гладко стало картам и как холодно "
    "от этой чужой аккуратности стае. Сегодняшний выбор — первая проба того, "
    "чьим коридором пойдёт остаток сезона."
)

_RECOUNT_BLOCK = (
    "ПЕРЕСЧЁТ. Сегодня второй звук, что стая слышала с первого дня, становится "
    "событием: за стенами проходит внеочередной пересчёт. Кто-то называет собак "
    "по одной, и счёт каждый раз сходится только до четвертой — пятой в его "
    "списке нет, и он не может это исправить. Пересчёт — не наказание и не угроза: "
    "это попытка найти ту, которой, по его записям, никогда не было. Пусть звук "
    "идёт из-за стен, как дыхание гиганта; стая его слышит, но ответа у неё нет. "
    "Одна деталь этого дня должна остаться у стаи в памяти — и вернуться в финале."
)


def villain_event(season_key_value: str, stage: int, salt: str = "") -> str:
    """Событие ступени плана. Соль делает перезапуски сезона разными:
    полный сброс стирает план, и новая арка начинается с других событий,
    хотя пул и тональность ступеней неизменны.

    Приоритет: AI cache > хардкод.
    """
    # Пытаемся взять из AI-кэша (сезон 1)
    ai_pool = get_villain_event_from_cache(1, stage)
    pool = list(ai_pool) if ai_pool and len(ai_pool) >= 3 else list(_VILLAIN_EVENTS.get(stage, _VILLAIN_EVENTS[0]))
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


# ---------- Правила Еретика: вторая сюжетная линия сезона ----------
#
# Зеркало плана Хозяина Ошибки. Если Хозяин хочет вернуть стае ровный
# предсказуемый сон (скука как победа), то Еретик — тот, кто заскучал
# первым и построил эту игру. Его линия объясняет механики мира как
# «изобретения»: закон Волка, глухие дни, эха вместо одноразовых
# последствий. События детерминированы по (сезон, ступень, слот ~4 дня),
# поэтому без состояния в БД: линия стабильна внутри окна и меняется
# между окнами и сезонами.

_HERETIC_EVENTS: dict[int, tuple[str, ...]] = {
    0: (
        "На стене старого приюта стая нашла выцарапанное правило, которого нет "
        "ни в одном архиве: «меньше голосов — сильнее след».",
        "Поверх чьей-то карты путей нарисована вторая тропа — торопливо, "
        "наспех, но упрямо, с маленьким апострофом в углу.",
        "Лайнер однажды оговорился о «том, кто принёс эти правила с собой» — "
        "и тут же сменил тему, будто проглотил язык.",
    ),
    1: (
        "Еретик впервые вышел к стае: короткий, колючий, с апострофом на "
        "ошейнике вместо имени. Ночь Одинокого Волка объявлена его правилом.",
        "Глухой день оказался не сбоем архива: печать на урне стоит с чужим "
        "клеймом — тем самым, что и на правиле Волка.",
        "Про старую Стаю он сказал одно слово — «скучно» — и отказался "
        "повторять дважды.",
    ),
    2: (
        "Из старых папок выпали письма: «здесь хотя бы миски полные. Твой "
        "новый закон — просто другой повод ошибиться». Еретик прочитал — "
        "и не выбросил.",
        "Выяснилось, что в старой игре Еретик голосовал каждый день: миллионы "
        "лап, один сон. Его собственного след там не нашли.",
        "Дневник сверил версии: в старом мире Еретик был первым, кто "
        "проголосовал против большинства, — и первым, кого за это не наказали.",
    ),
    3: (
        "У развилок теперь два набора знаков: ровные готовые решения — и "
        "торопливые правила с апострофом. Оба коридора идут к одному Лаю.",
        "Еретик предложил стае то, чего нет ни в старой игре, ни в плане "
        "Хозяина: выбрать финал, которого не знает даже он сам.",
        "«Он обещает вам порядок без ошибок, — сказал Еретик. — Я обещаю "
        "только право ошибаться своим следом».",
    ),
}

_HERETIC_STAGE_TONE = {
    0: "его имя ещё не звучит: мир полон примет, что правила здесь чьи-то",
    1: "Еретик назвался и вводит свои законы: сама механика мира — его почерк",
    2: "его прошлое догоняет: письма старой Стаи ставят под сомнение саму затею",
    3: "спор открыт: два плана, два коридора — и один Лай на двоих",
}


def heretic_event(anchor_key_value: str, stage: int, run_day: int) -> str:
    """Событие линии Еретика для окна ~4 дня забега.

    Детерминировано по (сезон, ступень, слот): внутри окна событие стабильно
    (канон не дёргается), между окнами ротируется по пулу, между сезонами
    различается солью якоря.

    Приоритет: AI cache > хардкод.
    """
    ai_pool = get_heretic_event_from_cache(1, stage)
    pool = list(ai_pool) if ai_pool and len(ai_pool) >= 3 else list(_HERETIC_EVENTS.get(stage, _HERETIC_EVENTS[0]))
    slot = max(0, run_day // 4)
    digest = hashlib.sha256(
        f"heretic:{anchor_key_value}:{stage}:{slot}".encode()
    ).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    return rng.choice(pool)


def heretic_prompt_block(
    anchor_key_value: str, stage: int, run_day: int, season: int | None = None
) -> str | None:
    """Блок «Правил Еретика» для промпта главы по образцу villain_prompt_block.

    Сезоны 2+: линия «первого, кого не досчитали». Еретик — не просто
    бунтарь, а тот, кого пересчёт однажды пропустил: его не «увёл» из старой
    Стаи — его стёрли из счёта, и он ушёл сам впервые заметив пустоту. Это
    раскрывается не как факт, а как тон (зеркало пересчёта Администратора).
    """
    event = heretic_event(anchor_key_value, stage, run_day)
    tone = _HERETIC_STAGE_TONE.get(stage, _HERETIC_STAGE_TONE[0])
    block = (
        f"ПРАВИЛА ЕРЕТИКА (канон, уже свершившееся): {event}\n"
        f"Текущая ступень его линии: {tone}. Вплетай одним касанием за главу "
        "(деталь, реплика или жест самого Еретика), не пересказывай и не "
        "объясняй его мотивы напрямую."
    )
    if season and season >= 2:
        block += (
            "\nПЕРЕСЧЁТ ЗНАЕТ ЕГО ИНАЧЕ: в этом сезоне стая может почувствовать — "
            "между Еретиком и пересчётом за стенами что-то общее. Он не восстал, "
            "его не сломали; он просто однажды заметил, что в счёте его нет, а "
            "счёт всё равно сходится. Показывай это как тихий холод, никак не "
            "называя — финал сам решит, станет ли это ответом на тайну."
        )
    return block


# ── AI-генерация событий злодея и еретика ──

_villain_events_cache: dict[str, list[str]] = {}
_heretic_events_cache: dict[str, list[str]] = {}


async def seed_villain_events(session, llm_caller=None, season: int = 1) -> int:
    """Генерирует события злодея для каждой ступени (0-3)."""
    from sqlalchemy import select as sa_select, func as sa_func
    from app.models import AIGeneratedPool

    inserted = 0
    for stage in range(4):
        q = (
            sa_select(sa_func.count())
            .select_from(AIGeneratedPool)
            .where(
                AIGeneratedPool.pool_type == "villain_events",
                AIGeneratedPool.season == season,
                AIGeneratedPool.phase == str(stage),
            )
        )
        result = await session.execute(q)
        if result.scalar() > 0:
            continue

        pool = list(_VILLAIN_EVENTS.get(stage, _VILLAIN_EVENTS[0]))
        is_ai = False
        if llm_caller:
            try:
                ai_pool = await _generate_villain_events_via_llm(stage, llm_caller)
                if ai_pool and len(ai_pool) >= 3:
                    pool = ai_pool
                    is_ai = True
            except Exception:
                pass

        row = AIGeneratedPool(
            pool_type="villain_events",
            season=season,
            phase=str(stage),
            content_json=json.dumps(pool, ensure_ascii=False),
            is_ai_generated=is_ai,
        )
        session.add(row)
        inserted += 1

    await session.commit()
    return inserted


async def _generate_villain_events_via_llm(stage: int, llm_caller) -> list[str] | None:
    """Генерирует события злодея через LLM."""
    _STAGE_DESC = {
        0: "Администратор только пробует мир на прочность — приметы мелкие, бытовые",
        1: "Его вмешательство стало явным — мир отвечает стае чужими решениями",
        2: "Он обращается к стае напрямую — послания, инвентаризации, полушаги",
        3: "Его ход сделан — план виден целиком, до финала сезона осталось дожить",
    }

    prompt = (
        f"Создай 5 событий для NPC «Администратор» в текстовой RPG.\n\n"
        f"Ступень сезона: {_STAGE_DESC.get(stage, stage)}\n\n"
        f"Контекст: постапокалиптический лабиринт, стая из 5 собак, тёмная атмосфера.\n"
        f"Каждое событие — 1-2 предложения, описывает действие Администратора.\n\n"
        f"Верни JSON-массив из 5 строк:\n"
        f'["событие 1", "событие 2", ...]'
    )

    messages = [{"role": "user", "content": prompt}]
    result = await llm_caller(messages, temperature=0.8, max_tokens=1000, want_json=True)

    if not result:
        return None

    data = unwrap_llm_json(result)
    if isinstance(data, list):
        return [str(s)[:500] for s in data if isinstance(s, str) and len(s) > 20][:5]
    if isinstance(data, dict) and isinstance(data.get("strings"), list):
        return [str(s)[:500] for s in data["strings"] if isinstance(s, str) and len(s) > 20][:5]

    return None


async def seed_heretic_events(session, llm_caller=None, season: int = 1) -> int:
    """Генерирует события еретика для каждой ступени (0-3)."""
    from sqlalchemy import select as sa_select, func as sa_func
    from app.models import AIGeneratedPool

    inserted = 0
    for stage in range(4):
        q = (
            sa_select(sa_func.count())
            .select_from(AIGeneratedPool)
            .where(
                AIGeneratedPool.pool_type == "heretic_events",
                AIGeneratedPool.season == season,
                AIGeneratedPool.phase == str(stage),
            )
        )
        result = await session.execute(q)
        if result.scalar() > 0:
            continue

        pool = list(_HERETIC_EVENTS.get(stage, _HERETIC_EVENTS[0]))
        is_ai = False
        if llm_caller:
            try:
                ai_pool = await _generate_heretic_events_via_llm(stage, llm_caller)
                if ai_pool and len(ai_pool) >= 3:
                    pool = ai_pool
                    is_ai = True
            except Exception:
                pass

        row = AIGeneratedPool(
            pool_type="heretic_events",
            season=season,
            phase=str(stage),
            content_json=json.dumps(pool, ensure_ascii=False),
            is_ai_generated=is_ai,
        )
        session.add(row)
        inserted += 1

    await session.commit()
    return inserted


async def _generate_heretic_events_via_llm(stage: int, llm_caller) -> list[str] | None:
    """Генерирует события еретика через LLM."""
    _STAGE_DESC = {
        0: "Его имя ещё не звучит — мир полон примет, что правила здесь чьи-то",
        1: "Еретик назвался и вводит свои законы — сама механика мира его почерк",
        2: "Его прошлое догоняет — письма старой Стаи ставят под сомнение саму затею",
        3: "Спор открыт — два плана, два коридора, и один Лай на двоих",
    }

    prompt = (
        f"Создай 5 событий для NPC «Еретик» в текстовой RPG.\n\n"
        f"Ступень сезона: {_STAGE_DESC.get(stage, stage)}\n\n"
        f"Контекст: постапокалиптический лабиринт, стая из 5 собак, тёмная атмосфера.\n"
        f"Каждое событие — 1-2 предложения, описывает действие Еретика.\n\n"
        f"Верни JSON-массив из 5 строк:\n"
        f'["событие 1", "событие 2", ...]'
    )

    messages = [{"role": "user", "content": prompt}]
    result = await llm_caller(messages, temperature=0.8, max_tokens=1000, want_json=True)

    if not result:
        return None

    data = unwrap_llm_json(result)
    if isinstance(data, list):
        return [str(s)[:500] for s in data if isinstance(s, str) and len(s) > 20][:5]
    if isinstance(data, dict) and isinstance(data.get("strings"), list):
        return [str(s)[:500] for s in data["strings"] if isinstance(s, str) and len(s) > 20][:5]

    return None


async def load_villain_events(session, season: int) -> None:
    """Загружает события злодея из БД в кэш."""
    global _villain_events_cache
    from sqlalchemy import select as sa_select
    from app.models import AIGeneratedPool

    for stage in range(4):
        key = f"villain:{season}:{stage}"
        if key not in _villain_events_cache:
            q = sa_select(AIGeneratedPool).where(
                AIGeneratedPool.pool_type == "villain_events",
                AIGeneratedPool.season == season,
                AIGeneratedPool.phase == str(stage),
            ).limit(1)
            result = await session.execute(q)
            row = result.scalar_one_or_none()
            if row:
                try:
                    _villain_events_cache[key] = json.loads(row.content_json)
                except Exception:
                    pass


async def load_heretic_events(session, season: int) -> None:
    """Загружает события еретика из БД в кэш."""
    global _heretic_events_cache
    from sqlalchemy import select as sa_select
    from app.models import AIGeneratedPool

    for stage in range(4):
        key = f"heretic:{season}:{stage}"
        if key not in _heretic_events_cache:
            q = sa_select(AIGeneratedPool).where(
                AIGeneratedPool.pool_type == "heretic_events",
                AIGeneratedPool.season == season,
                AIGeneratedPool.phase == str(stage),
            ).limit(1)
            result = await session.execute(q)
            row = result.scalar_one_or_none()
            if row:
                try:
                    _heretic_events_cache[key] = json.loads(row.content_json)
                except Exception:
                    pass


def get_villain_event_from_cache(season: int, stage: int) -> list[str] | None:
    """Возвращает события злодея из кэша."""
    key = f"villain:{season}:{stage}"
    return _villain_events_cache.get(key)


def get_heretic_event_from_cache(season: int, stage: int) -> list[str] | None:
    """Возвращает события еретика из кэша."""
    key = f"heretic:{season}:{stage}"
    return _heretic_events_cache.get(key)
