from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import time
from collections import deque
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.config import settings
from app.echoes import echo_prompt_lines
from app.lore import compose_chapter
from app.narrative_ai import (
    kolmogorov_ratio,
    should_retry_entropy,
    dynamic_temperature,
    coherence_score,
    sa_optimize_params,
    GenerationParams,
)


logger = logging.getLogger(__name__)


# Анти-репетиция: храним последние 10 начальных предложений для инжекта
# в промпт, чтобы модель не начинала карты одинаково.
_RECENT_OPENINGS: deque[str] = deque(maxlen=10)


def _estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов: ~4 символа на токен для смешанного рус/англ."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def text_entropy(text: str) -> float:
    """Shannon entropy — lower = more predictable."""
    import math
    from collections import Counter
    if not text:
        return 0.0
    freq = Counter(text.lower())
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


def bigram_diversity(text: str) -> float:
    """Ratio of unique bigrams — higher = more diverse."""
    words = text.split()
    if len(words) < 2:
        return 0.0
    bigrams = list(zip(words[:-1], words[1:]))
    return len(set(bigrams)) / len(bigrams)


def _check_word_frequency(text: str, max_per_1000: int = 8) -> list[str]:
    """Check for overused content words. Returns list of (word, count) tuples for words exceeding the limit."""
    import re
    from collections import Counter
    # Skip common Russian particles/prepositions/conjunctions
    _STOP = {
        "и", "в", "на", "с", "по", "не", "что", "как", "но", "да", "нет",
        "это", "он", "она", "оно", "они", "мы", "вы", "я", "ты", "к",
        "от", "до", "из", "за", "под", "над", "при", "для", "о", "у",
        "а", "ни", "ли", "бы", "же", "вот", "тут", "там", "где",
        "его", "её", "их", "мне", "тебе", "нам", "вам", "ей", "ему",
        "ей", "им", "них", "нем", "ней", "нём", "вас", "меня", "тебя",
        "себя", "свой", "мой", "твой", "наш", "ваш", "тот", "та", "те",
        "все", "вся", "всё", "каждый", "каждая", "каждое",
        "стая", "мир", "день", "дни", "путь", "мост", "кость",
        "ещё", "еще", "уже", "только", "если", "когда", "после",
        "один", "одна", "одно", "два", "три", "было", "будет",
    }
    words = re.findall(r"[а-яА-ЯёЁ]+", text.lower())
    length = max(len(words), 1)
    freq = Counter(w for w in words if w not in _STOP and len(w) > 3)
    overused = []
    for word, count in freq.most_common():
        if count > max_per_1000 * length / 1000:
            overused.append((word, count))
    return overused[:10]


# Voice cards: конкретные примеры речи для каждого персонажа.
# Инжектятся ТОЛЬКО когда персонаж появляется в сцене.
_VOICE_CARDS: dict[str, dict[str, str | list[str]]] = {
    "баркод": {
        "pattern": "считает вслух, короткие цифры без объяснений, одержим вероятностями",
        "examples": [
            "четыре… нет, пять… шанс был один к трём…",
            "семь — пять… если поставить ещё…",
            "двенадцать… нет, тринадцать… всегда тринадцать…",
        ],
        "banned": ["точка в конце числа", "отказ от ставки"],
    },
    "стежка": {
        "pattern": "короткие, точные фразы, знает то, что скрывает",
        "examples": [
            "там… а ты не спросил, откуда я знаю",
            "вон… правда не для всех",
            "тише…молчи, иначе стая услышит",
            "чую… но не скажу, что именно",
        ],
        "banned": ["длинные предложения", "прямая правда"],
    },
    "вектор": {
        "pattern": "громко, коротко, с повтором, ханжество — стоит против ветра, хотя ветер прав",
        "examples": [
            "Стой. Считай. Ещё раз. Я прав.",
            "Нет. Ещё. Раз. Мир ошибается.",
            "Стою. Вижу. Жду. Правда на моей стороне.",
        ],
        "banned": ["плавные фразы", "признание ошибки"],
    },
    "пиксель": {
        "pattern": "тихий, с паузами, гоняется за каждой искрой как за наркотиком",
        "examples": [
            "Я вижу… нет, подождите… вот… поймал… ещё одну…",
            "Там… да, вот оно… нельзя остановиться…",
            "Искра… поймал… но где предыдущая…",
        ],
        "banned": ["уверенные короткие фразы", "остановка"],
    },
    "безымянная": {
        "pattern": "почти не говорит, одна фраза решает, не доверяет никому",
        "examples": [
            "Там. Не спрашивай откуда.",
            "Нет. Я не верю.",
            "Идём. Но я иду последней.",
            "Это. Оно лжёт.",
        ],
        "banned": ["длинные объяснения", "вопросы", "доверие"],
    },
    "лайнер": {
        "pattern": "ласково, всегда с подвохом, торгует тем, что не его",
        "examples": [
            "считай, даром отдаю… почти даром… чужое, но тёплое…",
            "воспоминание за карту — выгодно, согласен?.. оно не твоё, но ты его уже помнишь…",
            "вот, держи… помнишь ли ты, что отдал?.. я помню за двоих…",
        ],
        "banned": ["прямые приказы", "бесплатная помощь"],
    },
    "архивариус": {
        "pattern": "канцелярский шёпот, НИКОГДА не ставит точку — только многоточие, говорит о расхождениях версий, полуправда хуже лжи",
        "examples": [
            "в папке написано одно… но стая помнит иначе… а я помню обе версии…",
            "расхождение между версиями — это не ошибка… это возможность… для кого-то…",
            "одна лупа показывает правду… другая — ту, что удобнее… вы выберете сами…",
        ],
        "banned": ["точка в конце предложения", "восклицательный знак", "однозначные утверждения", "полная правда"],
    },
    "еретик": {
        "pattern": "сухо, по-уставу, короткие правила, говорит о себе в третьем лице, переписал правила под себя",
        "examples": [
            "закон Волка — мой… я его написал…",
            "глухой день — тоже мой… стая думает, что выбрала свободу…",
            "Еретик решил — стая знала… нет, стая не знала…",
        ],
        "banned": ["вопросы от первого лица", "длинные объяснения", "признание egoism"],
    },
}


def _voice_cards_for(text: str) -> str:
    """Инжектит voice cards для персонажей, упомянутых в тексте."""
    low = (text or "").lower()
    blocks = []
    for char_key, card in _VOICE_CARDS.items():
        if char_key in low:
            examples = " | ".join(str(e) for e in card["examples"][:3])
            banned = card.get("banned", [])[:3]
            suffix = ""
            if banned:
                suffix = f" | ЗАПРЕЩЕНО: {', '.join(str(b) for b in banned)}"
            blocks.append(
                f"[ГОЛОС: {char_key.upper()} — {card['pattern']}. "
                f"Примеры: {examples}{suffix}]"
            )
    return "\n".join(blocks) if blocks else ""


# Структурированные «кристаллы памяти»: компактный JSON-снимок мира,
# который инжектится в промпт вместо сырых текстов канона.
# Формат: {events: [...], characters: {...}, promises: [...], world_facts: [...]}
def build_world_crystal(
    chapter: dict,
    day_index: int,
    win_rule=None,
    echoes=None,
) -> dict:
    """Строит структурированный снимок мира из данных главы."""
    crystal: dict = {
        "day": day_index,
        "events": [],
        "characters": {},
        "promises": [],
        "world_facts": [],
    }
    # Извлекаем события из текста главы
    text = str(chapter.get("text", ""))
    if text:
        # Простой парсинг: ищем упоминания персонажей
        char_names = {
            "баркод": "Баркод", "стежка": "Стежка", "вектор": "Вектор",
            "пиксель": "Пиксель", "безымянная": "Безымянная",
            "лайнер": "Лайнер", "архивариус": "Архивариус",
            "еретик": "Еретик", "администратор": "Администратор",
        }
        low = text.lower()
        for key, name in char_names.items():
            if key in low:
                crystal["characters"][name] = {"present": True}
    # Извлекаем ключевые факты из lore_summary
    summary = str(chapter.get("lore_summary", ""))
    if summary:
        crystal["world_facts"].append(summary[:200])
    # Обещания из описаний карт
    for card in chapter.get("cards") or []:
        consequence = str(card.get("consequence", ""))
        if consequence and len(consequence) > 20:
            crystal["promises"].append(consequence[:150])
    return crystal


def crystal_to_prompt(crystal: dict) -> str:
    """Конвертирует кристалл памяти в компактную строку для промпта."""
    parts = [f"День {crystal.get('day', '?')}"]
    if crystal.get("characters"):
        chars = ", ".join(crystal["characters"].keys())
        parts.append(f"Персонажи: {chars}")
    if crystal.get("promises"):
        promises = "; ".join(crystal["promises"][:2])
        parts.append(f"Обещания: {promises}")
    if crystal.get("world_facts"):
        facts = "; ".join(crystal["world_facts"][:2])
        parts.append(f"Факты: {facts}")
    return " | ".join(parts)


# Witness filter: не все персонажи знают обо всех событиях.
# Если событие произошло за кадром — только присутствующие знают о нём.
# Персонажи стаи (Баркод, Стежка, Вектор, Пиксель, Безымянная) всегда
# знают о событиях стаи. Лайнёр, Архивариус и Еретик — только если
# упомянуты в предыдущих битах.
_PACK_CHARS = {"баркод", "стежка", "вектор", "пиксель", "безымянная"}
_NPC_CHARS = {"лайнер", "архивариус", "еретик"}


def witness_filter(
    previous_beats: list[str],
    current_text: str,
) -> str:
    """Фильтрует знания NPC: добавляет инструкцию о том, кто что знает.

    Возвращает строку-инструкцию для промпта или пустую строку.
    """
    if not previous_beats:
        return ""
    # Определяем, какие NPC присутствовали в прошлых сценах
    recent_text = " ".join(previous_beats[-3:]).lower()
    present_npcs = set()
    for npc in _NPC_CHARS:
        if npc in recent_text:
            present_npcs.add(npc)
    # Если есть NPC, которых НЕ было в прошлых сценах — они не знают деталей
    all_npcs = _NPC_CHARS
    absent_npcs = all_npcs - present_npcs
    if not absent_npcs:
        return ""
    npc_names = {
        "лайнер": "Лайнер", "архивариус": "Архивариус", "еретик": "Еретик",
    }
    absent_names = [npc_names.get(n, n) for n in absent_npcs]
    return (
        f"СВИДЕТЕЛЬСТВО: {', '.join(absent_names)} НЕ присутствовал(и) в прошлых сценах — "
        "они не знают деталей вчерашних событий. Если упоминаешь их, "
        "они действуют на основе своих целей, а не на основе знания о недавних выборах стаи.\n"
    )


# Грубый стоп-фильтр: генерация с такими словами в чат не уходит,
# вызывающий код просто делает следующую попытку или берёт офлайн-текст.
# Без якоря \b: мат может быть спрятан внутри словоформ («охуело»).
_BANNED_RE = re.compile(
    r"(?i)("
    # русский мат во всех формах (по корню)
    r"х[уy][еёйy]|пизд|[еe]б[аaиint]|уёб|[оаoa][хx][уy][еeё]|муд[аa]к|гандон|гондон"
    r"|[сc]р[аa]ть|дроч|shit|fuck|bitch|whore|cunt"
    # слуры и призывы к ненависти
    r"|ниггер|n[i1]gger|faggot|педарас|пидор"
    r")",
)


def text_is_clean(text: str) -> bool:
    """True, если грубый фильтр не сработал. Выключается CONTENT_FILTER=false."""
    if not settings.content_filter:
        return True
    return _BANNED_RE.search(text or "") is None


# ---------- Типографика: нормализация вывода модели ----------

# Диалоговая кавычка-ёлочка вместо ASCII-апострофов и прямых кавычек;
# тире-диалог вместо дефиса с пробелами; многоточие одной литерой.
_DOUBLE_QUOTE_RE = re.compile(r'"([^"\n]{1,300}?)"')
_SINGLE_QUOTE_RE = re.compile(r"(?<![\wа-яёA-Z])'([^'\n]{1,300}?)'(?![\wа-яё])")
_DASH_RE = re.compile(r"(?<=\S) - (?=\S)")


def polish_typography(text: str) -> str:
    """Ёлочки, диалоговое тире и многоточие одним проходом.

    Модели стабильно выдают ASCII-кавычки и «...» — пост-обработка лечит
    ВСЕ будущие тексты одной функцией, не полагаясь на послушание модели.
    """
    if not text:
        return text
    result = _DOUBLE_QUOTE_RE.sub(lambda m: f"«{m.group(1)}»", text)
    result = _SINGLE_QUOTE_RE.sub(lambda m: f"«{m.group(1)}»", result)
    result = _DASH_RE.sub(" — ", result)
    return result.replace("...", "…")


def _polish_chapter(chapter: dict) -> dict:
    """Нормализует типографику всех русских полей главы на месте."""
    for key in ("title", "text", "lore_summary"):
        chapter[key] = polish_typography(str(chapter.get(key, "")))
    for card in chapter.get("cards") or []:
        for key in ("title", "description", "consequence"):
            card[key] = polish_typography(str(card.get(key, "")))
    return chapter


BASE_PROMPT = (
    "Ты — Ведущий (Dungeon Master) ежедневной сюжетной игры. Мир живёт и меняется от выборов игроков. "
    "Каждый день стая бездомных собак принимает решение, которое меняет лабиринт вокруг них. "
    "Веди игрока как настоящий Ведущий: второе лицо («ты»), настоящее время, живые сцены с прямой речью. "
    "Каждая развилка — трудная дилемма без очевидно правильного ответа, и каждое решение обязательно "
    "отзовётся позже, даже если не сразу.\n"
    "Пиши простым живым русским языком: короткие предложения, понятная причинность, никакого канцелярита, "
    "ломаного синтаксиса и случайной латиницы. Не повторяй одни и те же формулировки. "
    "Никаких метакомментариев и упоминаний нейросети: только художественный текст.\n"
    "Покажи, не назови: вместо «стая была голодна» — «желудок поджался так, что рёбра проступили». "
    "Каждое качество героя — через действие или деталь, не через прилагательное. "
    "Чередуй органы чувств: запах пыли, холод металла под лапами, привкус озона, шорох бумаги, "
    "тепло чужого тела рядом. Не ограничивайся звуком — у этого мира есть вкус и ощущение.\n"
    "Мир генерируется динамически: локации, персонажи, последствия — всё создаётся на основе действий "
    "стаи. Используй контекст мира из промпта для создания уникальных ситуаций."
)

CHARACTER_MICRO_PROMPTS: dict[str, str] = {
    "баркод": (
        "Баркод — тихий счётчик: молчит и считает. В глухой день показывает число дней до развилки. "
        "Помнит каждую кость, которую стая нашла или потеряла."
    ),
    "стежка": (
        "Стежка — чуткий следопыт: слышит еду раньше звука, чует всплывший след. "
        "Не спит, когда стая спит — обнюхивает туман."
    ),
    "вектор": (
        "Вектор — упрямый страж: пересчитывает чужие решения, не отступает. "
        "Говорит мало, но каждый его лай — приказ."
    ),
    "пиксель": (
        "Пиксель — ловец искр: ловит лапой искры порталов, читает цифры в тумане. "
        "Видит то, что другие считают случайностью."
    ),
    "безымянная": (
        "Безымянная — тихая тайна: чует Лай раньше других, знает, когда стена дышит. "
        "Её имя — ошибка, которую мир запомнил."
    ),
    "лайнер": (
        "Лайнер — торговец воспоминаниями: обменивает чужие долги на правду, помнит каждый долг стаи. "
        "Давно продал всю свою память и не помнит ничего — кроме пыльного радио, которое молчит с начала времён "
        "и оживает только в ночи кризиса ровным ЛАЕМ-сигналом. Говорит ласково и с подвохом («считай, даром отдаю… почти даром»)."
    ),
    "архивариус": (
        "Архивариус — хранитель лабиринта: знает все коридоры "
        "наизусть и выбирает, какой открыть сегодня. Не торговец — проводник. "
        "Фонарь в его лапе показывает не путь, а тот путь, который он выбрал за тебя. "
        "Ключи на поясе — от каждой двери, включая те, которые стая ещё не нашла. "
        "Говорит канцелярским шёпотом, НИКОГДА не ставит точку — только многоточие "
        "(«я открыл коридор для большинства… но запись помнит иначе…»)."
    ),
    "еретик": (
        "Еретик — пёс из старой Стаи: заскучал и увёл стаю переписывать правила. "
        "Говорит короткими формулами («закон Волка — мой»), "
        "его знак — апостроф. Под пальто прячет выцветший ошейник старой Стаи. Говорит о себе в третьем лице."
    ),
    "администратор": (
        "Администратор — тень без лица: пересчитывает стаю и чинит лабиринт, мечтая вернуть ровный сон. "
        "Не зол — тихо скучает. Его идеальная аккуратность всегда чуть грустная. Не говорит вовсе — "
        "о нём сообщают только последствия пересчётов."
    ),
    "крыса": (
        "Крыса — существо стен лабиринта: живёт в стёртых версиях дней, помнит все круги. "
        "Говорит живо, с цифровым шипением, жаргоном «котировок»: «забота сегодня недооценена». "
        "Продаёт дежавю-подсказки за память. На шее обглоданная табличка с инвентарным номером."
    ),
    "анубис": (
        "Анубис — судья цикла: высокий силуэт с головой шакала, золотые глаза, весы. "
        "Был до старой Стаи, взвешивает каждый круг. Одна фраза за появление: «Выбирали. Взвешу.»"
    ),
}

NPC_NAMES = {
    "лайнер", "архивариус", "еретик", "администратор", "крыса", "аллира", "анубис",
}


async def _build_dynamic_character_block(session) -> str:
    """Строит блок описаний персонажей из БД (AI-сгенерированных)."""
    from sqlalchemy import select
    from app.models import WorldCharacter

    try:
        q = select(WorldCharacter).where(WorldCharacter.is_alive == True).limit(10)
        result = await session.execute(q)
        characters = result.scalars().all()

        if not characters:
            return ""

        parts = ["ПЕРСОНАЖИ МИРА:"]
        for char in characters:
            mood_desc = {
                "neutral": "спокоен",
                "hostile": "враждебен",
                "friendly": "дружелюбен",
                "fearful": "испуган",
                "curious": "любопытен",
            }.get(char.mood, "непредсказуем")

            parts.append(
                f"- {char.name} ({char.role}): {char.personality[:100]}. "
                f"Настроение: {mood_desc}. Доверие к стае: {char.trust_stay}/10."
            )
            if char.flaw:
                parts.append(f"  Слабость: {char.flaw[:60]}")
            if char.virtue:
                parts.append(f"  Сила: {char.virtue[:60]}")

        return "\n".join(parts)
    except Exception:
        return ""


def _build_dynamic_prompt(text_blocks: tuple[str, ...] = ()) -> str:
    """Собирает системный промпт: база + микро-блоки для упомянутых NPC."""
    import re as _re
    text = " ".join(text_blocks).lower()
    parts = [BASE_PROMPT]
    for name in NPC_NAMES:
        if name in text:
            key = name if name in CHARACTER_MICRO_PROMPTS else name.split()[0]
            if key in CHARACTER_MICRO_PROMPTS:
                parts.append(CHARACTER_MICRO_PROMPTS[key])
    return "\n\n".join(parts)


def _build_scene_prompt(text_blocks: tuple[str, ...] = ()) -> str:
    """Динамический промпт для _free_story_llm с учётом персонажей сцены."""
    return _build_dynamic_prompt(text_blocks)


# Полный промпт для обратной совместимости (тесты, fallback)
DM_SYSTEM_PROMPT = _build_dynamic_prompt()

# Хвост стиля без фиксированной палитры: цвет приходит из арт-библии дня
# (палитра + якорь предыдущего дня), иначе ротация палитр между днями
# перечёркивается захардкоженным «teal and violet». Серия — flat 2D vector
# cozy-dystopia (см. промпт-пак): смелые контуры, матовые цвета, без живописи.
STYLE_SUFFIX = (
    ", flat 2D vector cartoon, cozy-dystopia, bold clean outlines, muted matte colors, "
    "glow of an open portal, no text, no letters, no watermark"
)


def styled_prompt(prompt: str) -> str:
    return prompt.strip().rstrip(",") + STYLE_SUFFIX


def _looks_like_image(content: bytes) -> bool:
    if len(content) < 5_000:
        return False
    png = content[:8] == b"\x89PNG\r\n\x1a\n"
    jpeg = content[:3] == b"\xff\xd8\xff"
    webp = len(content) > 12 and content[8:12] == b"WEBP"
    return png or jpeg or webp


# ---------- Предохранитель 429 (пер-модель, с потолком) ----------
# 429 у бесплатного Pollinations обычно распространяется на весь IP, но
# троттлит модели по-разному. Раньше единственная 429 глушила ВСЕ кадры дня
# и ВСЕ модели до истечения retry-after (иногда минуту+). Теперь охлаждение
# держится отдельно на модель-виновницу и ограничено потолком: соседняя
# модель из лестницы всё ещё может отдать кадр, а затяжной backoff не
# замораживает рендер всего дня до конца.

_THROTTLED_UNTIL: dict[str, float] = {}
_THROTTLE_CAP_SECONDS = 45


def _note_429(model: str, cooldown_seconds: int) -> None:
    import time as _time

    _THROTTLED_UNTIL[model] = _time.monotonic() + min(max(10, cooldown_seconds), _THROTTLE_CAP_SECONDS)


def _throttle_active(model: str | None = None) -> bool:
    import time as _time

    if model is not None:
        return _time.monotonic() < _THROTTLED_UNTIL.get(model, 0.0)
    return any(_time.monotonic() < until for until in _THROTTLED_UNTIL.values())


def _throttle_left_seconds(model: str | None = None) -> int:
    import time as _time

    if model is not None:
        return max(0, int(_THROTTLED_UNTIL.get(model, 0.0) - _time.monotonic()))
    return max([0] + [int(until - _time.monotonic()) for until in _THROTTLED_UNTIL.values()], default=0)


def _reset_throttle() -> None:
    """Очищает охлаждение (для тестов/ручного сброса)."""
    _THROTTLED_UNTIL.clear()


def _save_image(image: Image.Image, path: Path) -> None:
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        image.save(path, "JPEG", quality=88, optimize=True)
    else:
        image.save(path, "PNG", optimize=True)


# Потолок промпта для Pollinations: сверхдлинные урлы модель возвращает 400.
_PROMPT_CAP_CHARS = 1200


def _image_cache_path(
    model: str, seed: int, prompt: str, negative_prompt: str | None, width: int, height: int
) -> Path:
    """Путь кэша кадра: детерминирован по (model, seed, промпт).

    Возвращение стаи в то же место (тот же сид через place_seed_for) берёт
    готовый файл вместо повторной генерации — лимиты free-тира не жгутся.
    """
    raw = f"{model}|{seed}|{width}x{height}|{prompt}|{negative_prompt or ''}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]
    return Path(settings.media_dir) / "img_cache" / f"{digest}.jpg"


def _gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    strip = Image.new("RGB", (1, size[1]))
    for y in range(size[1]):
        t = y / max(size[1] - 1, 1)
        strip.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    return strip.resize(size)


def _ridge_layer(
    size: tuple[int, int], rng: random.Random, level: int, base: tuple
) -> Image.Image:
    """Силуэт горного хребта: ломаная с случайным рельефом, темнеет к переду."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    w, h = size
    base_y = int(h * (0.58 + 0.13 * level))
    amp = max(6, h // (9 + level * 3))
    points = [(0, base_y)]
    steps = 16
    for i in range(1, steps + 1):
        x = w * i // steps
        peak = base_y - rng.randint(amp // 2, amp)
        points.append((x, peak))
        points.append((x, base_y + rng.randint(-amp // 4, amp // 4)))
    points += [(w, h), (0, h)]
    shade = tuple(int(c * max(0.18, 0.42 - 0.11 * level)) for c in base)
    draw.polygon(points, fill=(*shade, 235))
    return layer


def _pack_silhouettes(
    size: tuple[int, int], rng: random.Random, base: tuple
) -> Image.Image:
    """Стая собак-путешественников идёт по гребню: простые чёрные силуэты."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    w, h = size
    ground = int(h * 0.72)
    ink = (max(base[0] - 14, 4), max(base[1] - 12, 3), max(base[2] - 10, 5), 255)
    count = rng.randint(3, 5)
    for index in range(count):
        cx = int(w * (0.14 + 0.72 * index / max(count - 1, 1)) + rng.randint(-w // 40, w // 40))
        s = max(10, min(w, h) // rng.randint(26, 34))
        cy = ground - rng.randint(0, h // 30)
        # корпус
        draw.ellipse((cx - s, cy - s * 0.45, cx + s, cy + s * 0.2), fill=ink)
        # голова и морда
        draw.ellipse((cx + s * 0.7, cy - s * 0.95, cx + s * 1.3, cy - s * 0.3), fill=ink)
        draw.polygon(
            [
                (cx + s * 0.78, cy - s * 0.9),
                (cx + s * 0.92, cy - s * 1.28),
                (cx + s * 1.02, cy - s * 0.82),
            ],
            fill=ink,
        )
        draw.rectangle(
            (cx + s * 1.2, cy - s * 0.52, cx + s * 1.48, cy - s * 0.34), fill=ink
        )
        # хвост
        draw.line(
            (cx - s * 0.95, cy - s * 0.2, cx - s * 1.45, cy - s * 0.75),
            fill=ink,
            width=max(3, s // 7),
        )
        # лапы
        leg_w = max(3, s // 6)
        for lx in (-0.65, -0.25, 0.35, 0.75):
            draw.rectangle(
                (
                    cx + s * lx,
                    cy + s * 0.05,
                    cx + s * lx + leg_w,
                    cy + s * 0.62,
                ),
                fill=ink,
            )
    return layer


def _abstract_scene(
    size: tuple[int, int],
    seed: str,
    base: tuple,
    accent: tuple,
    rings_center: tuple[float, float],
) -> Image.Image:
    """«Минималистичная тёмная сказка»: градиент неба, свечение портала,
    хребты в дымке и силуэт стаи на гребне. Без единой буквы — текст дня
    живёт в подписях Telegram, картинка остаётся картинкой."""
    rng = random.Random(seed)
    image = _gradient(size, base, tuple(max(c - 26, 0) for c in base))

    # Туманные пятна глубины.
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    secondary = (
        min(255, accent[0] + 30),
        min(255, accent[1] + 18),
        min(255, accent[2] + 40),
    )
    for _ in range(6):
        radius = rng.randint(min(size) // 5, min(size) // 2)
        x = rng.randint(-radius // 2, size[0] - radius // 2)
        y = rng.randint(-radius // 2, size[1] - radius // 2)
        color = accent if rng.random() < 0.6 else secondary
        alpha = rng.randint(36, 84)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(min(size) // 8))
    image = Image.alpha_composite(image.convert("RGBA"), glow)

    # Портал: концентрические кольца со светящимся ядром.
    rings = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(rings)
    cx, cy = int(rings_center[0] * size[0]), int(rings_center[1] * size[1])
    outer = int(min(size) * rng.uniform(0.24, 0.32))
    for delta in (0, outer // 7, outer // 3):
        r = outer - delta
        ring_width = max(4, outer // 22 - delta // 60)
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            outline=(*secondary, max(120, 190 - delta)),
            width=ring_width,
        )
    core_r = outer // 4
    draw.ellipse((cx - core_r, cy - core_r, cx + core_r, cy + core_r), fill=(*accent, 110))
    rings = rings.filter(ImageFilter.GaussianBlur(6))
    image = Image.alpha_composite(image, rings)

    # Хребты от дальнего к ближнему, между ними полосы тумана.
    for level in range(3):
        image = Image.alpha_composite(image, _ridge_layer(size, rng, level, base))
        if level < 2:
            fog = Image.new("RGBA", size, (0, 0, 0, 0))
            fdraw = ImageDraw.Draw(fog)
            band_y = int(size[1] * (0.60 + 0.13 * level))
            band_h = max(10, size[1] // 14)
            fdraw.rectangle(
                (0, band_y, size[0], band_y + band_h),
                fill=(min(255, accent[0] + 40), min(255, accent[1] + 34), min(255, accent[2] + 50), 46),
            )
            image = Image.alpha_composite(
                image, fog.filter(ImageFilter.GaussianBlur(band_h // 2))
            )

    # Стая на переднем плане.
    image = Image.alpha_composite(image, _pack_silhouettes(size, rng, base))

    # Виньетка.
    vignette = Image.new("L", size, 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse(
        (-size[0] // 4, -size[1] // 4, size[0] + size[0] // 4, size[1] + size[1] // 4),
        fill=255,
    )
    vignette = vignette.filter(ImageFilter.GaussianBlur(min(size) // 10))
    dark = Image.new("RGBA", size, (8, 6, 14, 150))
    image = Image.composite(image, dark, vignette)

    result = image.convert("RGB").filter(ImageFilter.GaussianBlur(0.4))
    # Плёночное зерно: лёгкий шум поверх всего кадра.
    grain = Image.effect_noise(size, 16).convert("RGB")
    return Image.blend(result, grain, 0.06)


def render_card(path: Path, title: str, description: str, position: int) -> None:
    """Локальный фолбэк карты пути: абстракция без текста."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bases = [(20, 16, 14), (12, 22, 26), (24, 15, 34)]
    accents = [(214, 118, 64), (84, 168, 168), (168, 96, 210)]
    centers = [(0.32, 0.34), (0.68, 0.42), (0.5, 0.62)]
    scene = _abstract_scene(
        (768, 1024),
        f"{title}|{position}",
        bases[position % 3],
        accents[position % 3],
        centers[position % 3],
    )
    _save_image(scene, path)


def render_cover(path: Path, title: str, body: str = "") -> None:
    """Локальный фолбэк обложки дня: абстракция без текста."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scene = _abstract_scene((1280, 720), f"{title}|cover", (17, 14, 13), (214, 118, 64), (0.5, 0.48))
    _save_image(scene, path)


async def _fetch_gemini_image(
    prompt: str, dest: Path, width: int = 768, height: int = 1024
) -> bool:
    """Первичный провайдер кадра: Google Gemini Image («nano banana»).

    Ключ GEMINI_API_KEY из AI Studio; free-tier квоты с запасом покрывают
    1-2 генерации в сутки (новая архитектура дня — один кадр). Ответ ищем в
    candidates[0].content.parts[*].inlineData (base64 PNG/JPEG). Любая
    неудача молча возвращает False — лестница продолжается Pollinations.
    """
    key = (settings.gemini_api_key or "").strip()
    if not settings.use_free_images or not key:
        return False
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_image_model}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": styled_prompt(prompt)}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    try:
        async with httpx.AsyncClient(timeout=settings.gemini_image_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers={"x-goog-api-key": key})
            if response.status_code != 200:
                logger.warning(
                    "Gemini image %s: HTTP %d — кадр уходит на следующего провайдера",
                    settings.gemini_image_model,
                    response.status_code,
                )
                return False
            data = response.json()
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        inline = next(
            (
                part.get("inlineData")
                for part in parts
                if isinstance(part, dict) and part.get("inlineData")
            ),
            None,
        )
        raw = str((inline or {}).get("data") or "")
        if not raw:
            logger.warning("Gemini image ответ без inlineData — лестница продолжается")
            return False
        content = base64.b64decode(raw)
        if not _looks_like_image(content):
            logger.warning("Gemini image вернул не изображение (%d байт)", len(content))
            return False
        image = Image.open(BytesIO(content)).convert("RGB")
        if image.size != (width, height):
            # fit вместо stretch: кадр обрезается по композиции, а не давится.
            image = ImageOps.fit(image, (width, height))
        dest.parent.mkdir(parents=True, exist_ok=True)
        _save_image(image, dest)
        logger.info("Кадр получен через Gemini (%s): %s", settings.gemini_image_model, dest.name)
        return True
    except Exception as exc:
        logger.warning("Gemini image не удался: %s", exc)
        return False


async def fetch_free_image(
    prompt: str,
    dest: Path,
    seed: int | None = None,
    width: int = 768,
    height: int = 1024,
    negative_prompt: str | None = None,
) -> bool:
    """Сцена дня от бесплатных моделей Pollinations. Несколько моделей и попыток:
    если сеть молчит, вызывающий код рисует локальный шаблон.

    negative_prompt — что модели рисовать запрещено (библия дня передаёт
    «no text, no watermark, no people» фолбэком). Кэш по (model, seed, промпт):
    возвращение в уже нарисованное место использует готовый файл, а не жжёт
    лимиты повторной генерацией. 429 не обрывает модель насовсем — короткая
    пауза и повтор, затем охлаждение передаёт эстафету следующей модели.
    """
    if not settings.use_free_images:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Потолок промпта: сверхдлинные у Pollinations давятся и возвращают
    # 400; режем по границе слова, а не посреди.
    if len(prompt) > _PROMPT_CAP_CHARS:
        cut = prompt.rfind(" ", 0, _PROMPT_CAP_CHARS)
        prompt = prompt[: cut if cut > 0 else _PROMPT_CAP_CHARS]
    base = "https://image.pollinations.ai/prompt/" + quote(styled_prompt(prompt))
    # Щедрые таймауты: бесплатная очередь flux иногда держит запрос минуту.
    long, mid = settings.image_timeout_seconds, max(45, settings.image_timeout_seconds - 25)
    plans = [
        ("flux", long, 1),
        ("sana", mid, 2),
        ("turbo", 45, 2),
    ]
    for model, seconds, attempts in plans:
        for attempt in range(1, attempts + 1):
            # Предохранитель 429: именно эта модель охлаждается. Не обрываем
            # весь кадр — соседняя модель (sana/turbo) может ещё отдать картинку.
            if _throttle_active(model):
                logger.warning(
                    "Pollinations %s охлаждается до %d с — пробуем другую модель",
                    model, _throttle_left_seconds(model),
                )
                break
            use_seed = seed if seed is not None else random.randint(1, 999999)
            cached = _image_cache_path(model, use_seed, prompt, negative_prompt, width, height)
            if cached.is_file():
                try:
                    import shutil

                    shutil.copyfile(cached, dest)
                    logger.info("Картинка взята из кэша: %s", dest.name)
                    return True
                except OSError:
                    pass
            url = (
                f"{base}?width={width}&height={height}&nologo=true&private=true&model={model}"
                f"&seed={use_seed}"
            )
            if negative_prompt:
                url += "&negative_prompt=" + quote(negative_prompt)
            if settings.pollinations_token:
                url += "&token=" + quote(settings.pollinations_token)
            try:
                async with httpx.AsyncClient(timeout=seconds, follow_redirects=True) as client:
                    response = await client.get(url)
                    if response.status_code == 429:
                        retry_after = response.headers.get("retry-after")
                        cool = int(retry_after) if retry_after and retry_after.isdigit() else 60
                        _note_429(model, cool)
                        logger.warning(
                            "Pollinations %s: 429 (retry-after %s) — пауза и повтор",
                            model, retry_after or "—",
                        )
                        # Короткая пауза и повтор той же модели: мимолётный троттл
                        # не должен перекидывать кадр на соседнюю модель зря.
                        await asyncio.sleep(min(max(5, cool), 20))
                        continue
                    if response.status_code != 200:
                        logger.warning("Pollinations %s: HTTP %d (попытка %d)", model, response.status_code, attempt)
                        continue
                    content = response.content
                    if not _looks_like_image(content):
                        logger.warning(
                            "Pollinations %s: ответ не изображение (%d байт, попытка %d)",
                            model,
                            len(content),
                            attempt,
                        )
                        continue
                    image = Image.open(BytesIO(content)).convert("RGB")
                    if image.size != (width, height):
                        image = image.resize((width, height))
                    _save_image(image, dest)
                    try:
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        _save_image(image, cached)
                    except OSError:
                        pass
                    logger.info("Картинка дня получена: %s (%d байт)", dest.name, len(content))
                    return True
            except Exception as exc:
                logger.warning("Pollinations %s (попытка %d) не удалась: %s", model, attempt, exc)
    return False


async def fetch_day_image(
    prompt: str,
    short_prompt: str,
    dest: Path,
    seed: int | None = None,
    width: int = 768,
    height: int = 1024,
    negative_prompt: str | None = None,
) -> bool:
    """Лестница кадра: Gemini «nano banana» → Pollinations (полный промпт,
    потом сжатый — длинные промпты иногда давят модель). False — вызывающий
    код рисует локальный абстракт. Один сетевой кадр в день делает лестницу
    практически безошибочной: ни один провайдер не успевает затроттлиться."""
    if await _fetch_gemini_image(prompt, dest, width=width, height=height):
        return True
    if await fetch_free_image(prompt, dest, seed=seed, width=width, height=height, negative_prompt=negative_prompt):
        return True
    if not settings.use_free_images:
        return False
    retry_seed = None if seed is None else seed + 9_000_001
    return await fetch_free_image(short_prompt, dest, seed=retry_seed, width=width, height=height, negative_prompt=negative_prompt)


async def generate_chapter(
    day_index: int,
    previous_beats: list[str],
    win_rule=None,
    echoes=None,
    distant_echoes: list[str] | None = None,
    season_block: str | None = None,
    places_block: str | None = None,
    villain_block: str | None = None,
    sealed: bool = False,
    pending_outcome: bool = False,
    salt: str = "",
    alignment_block: str | None = None,
    tint_lines: list[str] | None = None,
    focus_line: str | None = None,
    repeat_block: str | None = None,
    is_expanded: bool = False,
    active_scar_keys: set[str] | None = None,
    emotion_block: str | None = None,
    branches_block: str | None = None,
    dynamic_rules_block: str | None = None,
    needs_block: str | None = None,
) -> dict:
    authored = compose_chapter(
        day_index, previous_beats, win_rule, echoes, distant_echoes, season_block=season_block,
        villain_line=villain_block, sealed=sealed, pending_outcome=pending_outcome, salt=salt,
        tint_lines=tint_lines, focus_line=focus_line, active_scar_keys=active_scar_keys,
    )
    if not settings.use_free_story_llm:
        # Офлайн-глава тоже проходит полировку типографики (кавычки-ёлочки,
        # корректные тире), иначе рукописный офлайн и нейро-конспект жили по
        # разным правилам оформления.
        return _polish_chapter(authored)
    neural = await _free_story_llm(
        day_index, previous_beats, win_rule, echoes, distant_echoes,
        season_block=season_block, places_block=places_block,
        villain_block=villain_block, sealed=sealed, pending_outcome=pending_outcome,
        alignment_block=alignment_block,
        focus_line=focus_line,
        repeat_block=repeat_block,
        is_expanded=is_expanded,
        active_scar_keys=active_scar_keys,
        emotion_block=emotion_block,
        branches_block=branches_block,
        dynamic_rules_block=dynamic_rules_block,
        needs_block=needs_block,
    )
    # Типографика применяется к обоим путям: нейро-текст приходит с
    # ASCII-кавычками и дефисами, офлайн-сборка проходит для гарантии.
    return _polish_chapter(neural or authored)


class _LLMRateLimited(Exception):
    """Внутренний сигнал: провайдер сбросил на 429 — пробуем следующую модель."""


# Выключатель провайдеров: после нескольких сбоев подряд провайдер уходит на
# паузу, чтобы тик/шёпот/глава не долбили хост, который отвечает 429/5xx.
# Память в процессе — рестарт бота сбрасывает паузу, это приемлемо.
_PROVIDER_BREAKERS: dict[str, dict] = {}
_PROVIDER_OPEN_AFTER = 3  # сбоев подряд, прежде чем открыть выключатель
_PROVIDER_COOLDOWN = 120.0  # секунд «холода» провайдера


def _breaker_status(base_url: str) -> bool:
    """True, если выключатель открыт — провайдер на паузе, его пропускаем."""
    state = _PROVIDER_BREAKERS.get(base_url)
    return state is not None and time.monotonic() < state.get("open_until", 0.0)


def _breaker_note(base_url: str, ok: bool) -> None:
    """Регистрирует исход попытки: успех закрывает, сбой копит к открытию."""
    state = _PROVIDER_BREAKERS.setdefault(base_url, {"fails": 0, "open_until": 0.0})
    if ok:
        state["fails"] = 0
        state["open_until"] = 0.0
        return
    state["fails"] += 1
    if state["fails"] >= _PROVIDER_OPEN_AFTER:
        state["open_until"] = time.monotonic() + _PROVIDER_COOLDOWN
        logger.warning(
            "LLM-провайдер %s на паузе %s с (выключатель открыт)", base_url, _PROVIDER_COOLDOWN
        )


async def _chat_completion(
    messages: list[dict],
    timeout: int | None = None,
    *,
    temperature: float = 0.85,
    max_tokens: int = 3500,
    want_json: bool = False,
) -> tuple[dict, str] | None:
    """OpenAI-совместимый запрос по цепочке провайдеров и моделей.

    Если задан LLM_API_KEY — сначала кастомный провайдер (Hugging Face, Groq,
    OpenRouter, локальная Ollama), затем бесплатный Pollinations. Первый
    валидный ответ побеждает; иначе None и вызывающий код уходит в офлайн-лор.

    temperature/max_tokens — настройки per-call (арт-библия холоднее и короче
    главы). want_json включает response_format json_object ТОЛЬКО на ключевом
    провайдере: бесплатный Pollinations на него отвечает 400, и это отдельный
    путь фолбэка.

    Устойчивость здесь общая для ВСЕХ текстовых генераторов (эпилог, открывающее
    эхо, тизер, шёпот, арт-библия — у части из них отдельных повторов нет вовсе):
      - 429: читаем retry-after (с потолком) и переходим к следующей модели,
        не обрушивая весь вызов;
      - 400 на response_format: повтор ТОГО ЖЕ запроса без json-режима;
      - выключатель провайдера: несколько сбоев подряд (429/ошибка сети) уводят
        хост на паузу `_PROVIDER_COOLDOWN`, каждый звонок его не долбит;
      - полный сбой цепочки: один повтор всего провайдера после короткой паузы,
        чтобы краткий сетевой blip не обнулял генерацию.
    """
    if timeout is None:
        timeout = settings.llm_timeout_seconds
    providers: list[tuple[str, str, list[str]]] = []
    if settings.llm_api_key:
        providers.append((settings.llm_base_url, settings.llm_api_key, settings.llm_model_chain))
    pollinations_url = "https://text.pollinations.ai/openai"
    pollinations_key = ""
    if settings.pollinations_token:
        # Токен — и в query (?token=), и как Bearer: разные версии эндпоинта
        # читают из разных мест; анонимный общий IP Render ловит 402.
        pollinations_url += "?token=" + quote(settings.pollinations_token)
        pollinations_key = settings.pollinations_token
    providers.append((pollinations_url, pollinations_key, settings.story_model_chain))
    for overall_attempt in range(1, 3):
        for base_url, key, models in providers:
            if _breaker_status(base_url):
                logger.info("LLM %s в паузе — пропуск (выключатель открыт)", base_url)
                continue
            for model in models:
                body: dict = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "frequency_penalty": settings.llm_frequency_penalty,
                    "presence_penalty": settings.llm_presence_penalty,
                }
                if want_json and key:
                    body["response_format"] = {"type": "json_object"}
                try:
                    headers = {"Authorization": f"Bearer {key}"} if key else {}
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.post(
                            base_url, json=body, headers=headers,
                        )
                        if response.status_code == 429:
                            retry_after = response.headers.get("retry-after", "")
                            pause = min(int(retry_after), 10) if retry_after.isdigit() else 4
                            logger.warning("LLM %s @ %s: 429 — пауза %d с, следующая модель", model, base_url, pause)
                            await asyncio.sleep(pause)
                            raise _LLMRateLimited()
                        if response.status_code == 400 and "response_format" in body:
                            logger.warning(
                                "LLM %s @ %s: 400 на json-режим — повтор без него", model, base_url
                            )
                            fallback_body = {k: v for k, v in body.items() if k != "response_format"}
                            response = await client.post(
                                base_url, json=fallback_body, headers=headers,
                            )
                        response.raise_for_status()
                        _breaker_note(base_url, True)
                        return response.json(), model
                except _LLMRateLimited:
                    _breaker_note(base_url, False)
                    continue
                except Exception as exc:
                    _breaker_note(base_url, False)
                    logger.warning("LLM %s @ %s не ответил: %s", model, base_url, exc)
                    continue
        if overall_attempt == 1:
            logger.warning("Все модели LLM недоступны — повтор цепочки через 3 с")
            await asyncio.sleep(3)
    return None


def _chapter_text_fields(data: dict) -> list[str]:
    parts = [str(data.get("title", "")), str(data.get("text", "")), str(data.get("lore_summary", ""))]
    for card in data.get("cards") or []:
        for key in ("title", "description", "consequence"):
            parts.append(str(card.get(key, "")))
    return parts


def _parse_chapter(payload: dict, day_index: int) -> dict | None:
    content = payload["choices"][0]["message"]["content"]
    data = _extract_json(content)
    cards = data.get("cards") or []
    if len(cards) != 3:
        return None
    if not text_is_clean(" ".join(_chapter_text_fields(data))):
        logger.warning("Глава дня отброшена стоп-фильтром")
        return None
    if isinstance(data.get("place"), str):
        data["place"] = data["place"].strip()[:80] or None
    else:
        data["place"] = None
    data.setdefault(
        "cover_prompt",
        f"flat 2D vector cartoon, cozy-dystopia, bold clean outlines, muted matte colors, "
        f"wide shot, day {day_index} of a portal-hopping stray dog pack saga, "
        "glowing unstable gateway, dusty teal and burnt orange palette, no text",
    )
    for card in cards:
        tag = card.get("tag")
        card["tag"] = tag if tag in {"risk", "care", "cunning"} else "care"
    # Порядок карт перемешивается детерминированно: иначе модели почти всегда
    # возвращают риск/забота/хитрость по порядку, и Путь I становится предсказуемым.
    order_rng = random.Random(f"cardorder:{day_index}:{data.get('title', '')}")
    order_rng.shuffle(cards)
    return data


def _build_story_prompt(
    day_index: int,
    previous_beats: list[str],
    win_rule=None,
    echoes=None,
    distant_echoes: list[str] | None = None,
    season_block: str | None = None,
    places_block: str | None = None,
    villain_block: str | None = None,
    sealed: bool = False,
    pending_outcome: bool = False,
    alignment_block: str | None = None,
    focus_line: str | None = None,
    repeat_block: str | None = None,
    is_expanded: bool = False,
    active_scar_keys: set[str] | None = None,
    emotion_block: str | None = None,
    branches_block: str | None = None,
    dynamic_rules_block: str | None = None,
    needs_block: str | None = None,
) -> str:
    """Промпт главы дня. Чистая функция — покрывается тестами без сети."""
    history = "\n".join(previous_beats[-8:]) or "история ещё не началась"
    law_line = ""
    if win_rule is not None:
        from app.models import RULE_PHRASES

        if sealed:
            law_line = (
                "ЗАКОН ДНЯ ЗАПЕЧАТАН: игроки его не знают до итогов, им показан "
                "только хеш-обязательство. Ни в главе, ни в картах нельзя называть, "
                "какой сегодня закон (большинство/меньшинство/среднее). Вместо этого "
                "обыграй тайну: Архивариус запечатал урну и ухмыляется, персонажи "
                "спорят и гадают вслух, куда сегодня пойдёт архив. Сцена должна "
                "нагнетать двойную интригу: неясно и куда пойдёт стая, и что решит архив.\n"
            )
        else:
            law_line = (
                f"Закон сегодняшнего дня уже объявлен игрокам с утра: {RULE_PHRASES[win_rule]}. "
                "В главе он должен прозвучать голосом Архивариуса как реплика в сцене "
                "(канцелярский шёпот, полуправда), а не сухой справкой за кадром. "
                "ЗАПРЕЩЕНО цитировать формулировку дословно и называть механику "
                "(«среднее число голосов», «большинство», «меньшинство») — "
                "Архивариус передаёт закон образом архива и меры, игрок поймёт.\n"
            )
    villain_text = ""
    if villain_block:
        villain_text = f"{villain_block}\n"
    echo_block = ""
    if echoes:
        echo_block = (
            "Детали прошлых дней (вплети каждую в текст главы и минимум одну — "
            "в карту: в описание или последствие). Делай это незаметно, как "
            "естественную примету мира: не упоминай номера дней, голосования, "
            "и не используй слова «эхо», «отголосок», «как раньше», «снова». "
            "Игроки должны сами узнать повтор, если помнят:\n"
            + "\n".join(echo_prompt_lines(echoes)) + "\n"
        )
    distant_block = ""
    if distant_echoes:
        distant_block = (
            "Давний канон (дни старше двух недель; мир сам их вспомнил, потому "
            "что они похожи на сегодняшнюю ситуацию). Вплети максимум одну из "
            "этих строк лёгким касанием — одной фразой в тексте главы, без "
            "пересказа целиком:\n"
            + "\n".join(f"- {line}" for line in distant_echoes) + "\n"
        )
    season_text = f"{season_block}\n" if season_block else ""
    # Пролог и серединный поворот несут двойную нагрузку (сцена + знакомство/
    # событие): просим у модель более длинную главу. Пост выдерживает
    # до ~3200 знаков текста при лимите Telegram 3900 на весь пакет.
    chapter_low, chapter_high = (1400, 1700) if is_expanded else (1200, 1500)
    villain_text = villain_text if villain_block else ""
    # alignment_block уже внутри season_text (через season.py:527),
    # но если season_block передан без него — добавляем отдельно.
    align_text = ""
    if alignment_block and alignment_block not in (season_block or ""):
        align_text = f"{alignment_block}\n"
    # Анти-репетиция: список последних начальных предложений для избегания
    avoid_block = ""
    if _RECENT_OPENINGS:
        avoid_block = (
            "ИЗБЕГАЙ этих начал карточек (используй совершенно другие формулировки): "
            + "; ".join(f"«{o}»" for o in list(_RECENT_OPENINGS)[-5:]) + "\n"
        )
    # Voice cards: инжектим конкретные примеры речи для персонажей в сцене
    voice_block = _voice_cards_for(
        f"{previous_beats} {season_text} {villain_text} {echo_block} "
        f"{alignment_block} {focus_line} {repeat_block}"
    )
    # Witness filter: не все NPC знают о прошлых событиях
    witness_block = witness_filter(previous_beats, history)
    places_text = ""
    if places_block:
        places_text = (
            "Память мест (сеть помнит географию маршрута). Если стая сегодня "
            "возвращается в одно из этих мест — покажи, что здесь изменилось с "
            "тех пор: место до сих пор носит отпечаток того выбора. Название "
            "вернувшегося места укажи в поле place.\n"
            + places_block + "\n"
        )
    repeat_text = f"{repeat_block}\n" if repeat_block else ""
    # Шрамы мира: активные шрамы влияют на локации и тон
    scar_text = ""
    if active_scar_keys:
        scar_descriptions = {
            "burned_path": "стая сожгла мост — мир помнит дым",
            "scorched_earth": "слишком много мостов сожжено — земля выжжена",
            "fresh_wound": "свежая рана — мир помнит боль",
            "warm_hearth": "стая создала тёплый очаг — место, куда хочется возвращаться",
            "sanctuary": "стая стала домом для других — святилище",
            "gentle_breath": "мягкое дыхание — мир стал теплее",
            "labyrinth_doubt": "сомнение лабиринта — коридоры дублируются",
            "false_trails": "ложные тропы — хитрость открыла новые коридоры",
            "whisper_of_trick": "шёпот обмана — кто-то считает дни иначе",
        }
        scar_lines = [f"- {scar_descriptions.get(k, k)}" for k in active_scar_keys if k in scar_descriptions]
        if scar_lines:
            scar_text = (
                "Шрамы мира (вплети в текст главы и локации): "
                "выборы стаи оставили следы в лабиринте. "
                "Не называй слово «шрам» — покажи последствия образами:\n"
                + "\n".join(scar_lines) + "\n"
            )
    # GEPA: динамический промпт от эволюционного гена (из module-level cache)
    _gepa_block = ""
    try:
        from app.narrative_ai import get_active_gene
        _gene = get_active_gene()
        if _gene is not None:
            _gepa_block = _gene.to_prompt_block() + "\n"
    except Exception:
        pass
    head = (
        "Ответь только JSON. Русский язык. Ежедневная сюжетная игра в духе D&D. "
        f"День {day_index}. Канон прошлых дней:\n{history}\n"
        f"{law_line}"
        f"{season_text}"
        f"{align_text}"
        f"{focus_line + chr(10) if focus_line else ""}"
        f"{villain_text}"
        f"{echo_block}"
        f"{distant_block}"
        f"{avoid_block}"
        f"{voice_block}"
        f"{witness_block}"
        f"{places_text}"
        f"{repeat_text}"
        f"{scar_text}"
        f"{emotion_block + chr(10) if emotion_block else ''}"
        f"{branches_block + chr(10) if branches_block else ''}"
        f"{dynamic_rules_block + chr(10) if dynamic_rules_block else ''}"
        f"{needs_block + chr(10) if needs_block else ''}"
        f"{_gepa_block}"
        "Напиши главу дня — цельный рассказ на "
        f"{chapter_low}-{chapter_high} знаков, от второго "
        "лица и в настоящем времени. Это история самой стаи игрока, а не чужих "
        "героев: Баркод, Стежка, Вектор, Пиксель и Безымянная — только фоновый "
        "бросок, новых главных персонажей не вводи. В дни пролога фокус сцены — "
        "одно вводимое лицо; остальные постоянные лица молчат фоном без реплик.\n"
        "Обязательный состав главы, по порядку:\n"
    )
    if pending_outcome:
        # Пережиток двухфазной прегенерации: глава собиралась в час подсчёта,
        # до вскрытия урны, и отголосок дописывал отдельный короткий вызов
        # после итогов. В инлайн-днях параметр всегда False; ветка сохранена
        # для совместимости тестов собирателя.
        opening_line = (
            "(1) Вступление-отголосок будет дописано позже отдельным вызовом — "
            "НЕ пиши его. Начинай сразу со сцены «сейчас», не упоминая "
            "вчерашний выбор и его исход;\n"
        )
    elif not previous_beats:
        # Пустой канон (первый день мира после сброса): модель любила
        # сочинять «вчерашний камешек» — запрещаем явно.
        opening_line = (
            "(1) Канон пуст: это первый день этого мира. НЕ выдумывай "
            "вчерашних событий и старых следов — начинай сразу со сцены "
            "«сейчас»;\n"
        )
    else:
        opening_line = (
            "(1) Отголосок вчера: чем отозвался вчерашний выбор из канона — через "
            "одну КОНКРЕТНУЮ деталь мира или стаи: предмет, шрам на местности, "
            "запах, постройку. ЗАПРЕЩЕНЫ мета-фразы вроде «напоминает о "
            "вчерашнем выборе» и любые отсылки к факту голосования — только "
            "то, что изменилось в мире;\n"
        )
    # Компактный профиль: карты целиком влезают в развилку поста (показ без
    # многоточий до 260 знаков) — потолок промпта обязан быть ниже него.
    card_desc_budget = 280
    return (
        head
        + opening_line
        + "(2) Сцена сейчас: куда стая вышла сегодня; живая сенсорная деталь "
        "(звук портала, свет мисок, шёпот папок) и минимум одна прямая реплика "
        "персонажа с его классовым стилем (Рейнджер считает, Разбойник чувствует, "
        "Варвар кричит, Оккультист видит, Варлок молчит, Бард торгует, Жрец "
        "объявляет, Палладин.rules);\n"
        "(3) Закон дня звучит голосом Архивариуса как реплика в сцене — с его "
        "полуправдой и канцелярским шёпотом, а не сухой справкой;\n"
        "(4) Напряжение выбора: конкретная дилемма «вагонетки» — стая должна "
        "выбрать, кого спасти, чем пожертвовать, какой ценой заплатить. "
        "Три пути: каждый с конкретной ценой, каждый оставляет след. "
        "Финальная строка главы — крючок: недоговорённость, звук или вопрос, "
        "обрывающий сцену перед картами. Не резюмируй мораль.\n"
        "Напряжение нарастает от спокойного начала к моменту обнаружения или опасности — не выдавай выбор сразу. "
        "Начни тихо, дай почуять мир, потом введи сбой или тревогу, и только потом — дилемму. "
        "Чередуй короткие рубленые предложения с длинными. Паузу перед выбором можно выразить одним словом на абзац. "
        "НЕ заканчивай моралью, резюме или прямым обращением к игроку — последнее слово главы должно быть незавершённым.\n"
        "Три карты — дилемма «вагонетки»: конкретная ситуация, где нужно выбрать: "
        "кого спасти, чем пожертвовать, какой ценой заплатить. Без очевидно "
        "правильного ответа. Варьируй тип дилеммы ото дня ко дню: иногда "
        "нужно выбрать между двумя группами, иногда — между безопасностью и "
        "свободой, иногда — между правдой и ложью. Каждая карта — одно "
        "конкретное действие с понятной ценой: что стая сделает, кому поможет "
        "и чем за это заплатит.\n"
        "ИНВЕНТАРЬ: если стая нашла предмет, информацию или союзника — "
        "упомяни это в описании пути или последствии. Если потеряла — тоже.\n"
        "Правила ясности. Пиши простым живым русским языком, короткими "
        "предложениями; причина и следствие обязаны сходиться. Не оставляй "
        "двусмысленных местоимений: после «и» читателю ясно, кто выполняет "
        "действие. Архивные листы называй папками или страницами; словом "
        "«карта» — только пути выбора. Запрещено: вставлять одну и ту же фразу "
        "или название места во все три карты; повторять формулировки главы в "
        "картах дословно; канцелярит, англицизмы, ломаный синтаксис; обращения "
        "к игроку как к читателю и мораль после выбора. Не упоминай "
        "голосование и механику игры в тексте.\n"
        f"Описание карты — короткое, не больше {card_desc_budget} знаков: "
        "действие, цена и след, который оно оставит. Последствие — одно-два "
        "предложения в формате «обещание + "
        "угроза»: что стая получит и чем за это заплатит; оно завтра станет "
        "каноном.\n"
            'Мини-пример формы ответа (СОКРАЩЁН, значения выдуманы — не копируй их): '
        '{"title":"День 9. Тихий порт","place":"Тихий порт","text":"…","lore_summary":"…","cover_prompt":"wide shot, …","cards":[{"title":"…","description":"…","consequence":"обещание + угроза","tag":"risk"},{},{},{}]}. '
    'Формат: {"title":"День N. ...","place":"короткое название места дня",'
        f'"text":"история дня, {chapter_low}-{chapter_high} знаков",'
        '"lore_summary":"...",'
        '"cover_prompt":"english wide cinematic scene summarizing the whole day",'
        '"cards":[{"title":"...","description":"...","consequence":"...",'
        '"tag":"risk|care|cunning"},{},{}]}. '
        "Ровно 3 карты: риск, забота, хитрость. Ссылайся на прошлый канон."
    )




def _check_violations(text: str) -> list[str]:
    """Проверяет сгенерированный текст на нарушения голосовых карточек."""
    violations: list[str] = []
    low = text.lower()
    # Архивариус: диалоги не должны заканчиваться точкой (только многоточие)
    for match in re.finditer(
        r'[«"]([^»"]*)[»"]', text,
    ):
        snippet = match.group(1)
        if "архивариус" in snippet.lower() and snippet.endswith("."):
            violations.append(
                f"Архивариус заканчивает точкой: «{snippet[:60]}…»"
            )
    # Безымянная: диалоги длиннее 50 символов
    for match in re.finditer(
        r'[«"]([^»"]*)[»"]', text,
    ):
        snippet = match.group(1)
        if "безымянная" in snippet.lower() and len(snippet) > 50:
            violations.append(
                f"Безымянная: диалог >50 символов ({len(snippet)}): «{snippet[:40]}…»"
            )
    # Еретик: говорит о себе от первого лица (я, мне, мой)
    for match in re.finditer(
        r'[«"]([^»"]*)[»"]', text,
    ):
        snippet = match.group(1)
        if "еретик" in snippet.lower():
            first_person = re.search(
                r'\b(я|мне|мой|моя|моё|мои)\b', snippet, re.IGNORECASE,
            )
            if first_person:
                violations.append(
                    f"Еретик от первого лица: «{snippet[:60]}…»"
                )
    return violations


async def _free_story_llm(
    day_index: int,
    previous_beats: list[str],
    win_rule=None,
    echoes=None,
    distant_echoes: list[str] | None = None,
    season_block: str | None = None,
    places_block: str | None = None,
    villain_block: str | None = None,
    sealed: bool = False,
    pending_outcome: bool = False,
    alignment_block: str | None = None,
    focus_line: str | None = None,
    repeat_block: str | None = None,
    is_expanded: bool = False,
    active_scar_keys: set[str] | None = None,
    emotion_block: str | None = None,
    branches_block: str | None = None,
    dynamic_rules_block: str | None = None,
    needs_block: str | None = None,
) -> dict | None:
    prompt = _build_story_prompt(
        day_index, previous_beats, win_rule, echoes, distant_echoes,
        season_block=season_block, places_block=places_block,
        villain_block=villain_block, sealed=sealed, pending_outcome=pending_outcome,
        alignment_block=alignment_block,
        focus_line=focus_line,
        repeat_block=repeat_block,
        is_expanded=is_expanded,
        active_scar_keys=active_scar_keys,
        emotion_block=emotion_block,
        branches_block=branches_block,
        dynamic_rules_block=dynamic_rules_block,
        needs_block=needs_block,
    )
    # Динамический промпт: подбираем NPC под сцену
    _text_blocks = (
        season_block or "", villain_block or "", alignment_block or "",
        focus_line or "", " ".join(previous_beats[-3:]),
    )
    _system_prompt = _build_scene_prompt(_text_blocks)
    _old_tokens = _estimate_tokens(DM_SYSTEM_PROMPT)
    _new_tokens = _estimate_tokens(_system_prompt)
    if _new_tokens < _old_tokens:
        logger.debug(
            "Динамический промпт дня %d: %d → %d токенов (-%d%%)",
            day_index, _old_tokens, _new_tokens,
            int((1 - _new_tokens / _old_tokens) * 100),
        )
    messages = [
        {"role": "system", "content": _system_prompt},
        {"role": "user", "content": prompt},
    ]
    # Контроль бюджета токенов: предупреждаем, если промпт приближается
    # к лимиту контекста (типичный free-модельный лимит ~8k-12k токенов).
    total_tokens = _new_tokens + _estimate_tokens(prompt)
    if total_tokens > 6000:
        logger.warning(
            "Промпт дня %d: ~%d токенов (system %d + user %d) — близко к лимиту модели",
            day_index, total_tokens,
            _new_tokens, _estimate_tokens(prompt),
        )
    # Одна повторная попытка всей цепочки: битый JSON у бесплатных моделей —
    # обычное дело, лимит это позволяет.
    # Полный отказ сети — не приговор: повторная попытка всей цепочки после
    # короткой паузы. Раньше код возвращал None сразу (вопреки замыслу), и
    # краткий сетевой сбой уводил день в офлайн-лор без нужды.
    _sa_params: GenerationParams | None = None
    _candidate_texts: list[str] = []
    _current_temp = settings.llm_temperature
    for attempt in range(1, 4):
        result = await _chat_completion(messages, temperature=_current_temp)
        if result is None:
            if attempt == 1:
                logger.warning("Все модели недоступны (сетевой сбой) — повтор через 5 с")
                await asyncio.sleep(5)
                continue
            return None
        payload, used_model = result
        try:
            data = _parse_chapter(payload, day_index)
            if data is not None:
                # Контроль длины (инцидент: модель отдала конспект на ~1000
                # знаков, и он ушёл в пост). Нейро-глава обязана набрать
                # контрактный минимум; ниже — отклонение, вызывающий код
                # соберёт офлайн-версию (её аварийный пол ниже: 1000).
                expanded = bool(season_block) and (
                    "ПРОЛОГ" in season_block or "ПОВОРОТ СЕРЕДИНЫ" in season_block
                )
                min_chars = 1000 if expanded else 850
                text_len = len(str(data.get("text", "")))
                if text_len < min_chars:
                    logger.warning(
                        "Модель %s вернула главу %d знаков (<%d, попытка %d) — отклонена",
                        used_model, text_len, min_chars, attempt,
                    )
                    continue
                # Верхний потолок: болезненно длинная глава режется по границе
                # предложения, а не заводит день с простыней и риском обрыва в ТГ.
                data["text"] = _clamp_sentence(str(data.get("text", "")), 2600)
                # Проверка диверситета: если bigram_diversity < 0.55 — предупреждаем
                # (не отклоняем, чтобы не ломать тесты с коротким контентом)
                _text_check = str(data.get("text", ""))
                if len(_text_check.split()) > 20:
                    _div = bigram_diversity(_text_check)
                    if _div < 0.55:
                        logger.warning(
                            "Низкий диверситет дня %d: %.2f < 0.55 (попытка %d)",
                            day_index, _div, attempt,
                        )
                # Word frequency cap: flag overused content words
                _overused = _check_word_frequency(str(data.get("text", "")))
                if _overused:
                    logger.warning(
                        "Частотные слова дня %d: %s (попытка %d)",
                        day_index, _overused, attempt,
                    )
                # Анти-репетиция: запоминаем первые предложения описаний карт
                for card in data.get("cards") or []:
                    desc = str(card.get("description", "")).strip()
                    if desc:
                        # Берём первые 80 символов как «начало»
                        opening = desc[:80].rsplit(" ", 1)[0] if len(desc) > 80 else desc
                        _RECENT_OPENINGS.append(opening)
                # Проверка диверситета карт: если 2 из 3 карт совпадают по >50% биграмм
                _cards = data.get("cards") or []
                if len(_cards) >= 2:
                    _card_texts = [str(c.get("description", "")) for c in _cards]
                    for i in range(len(_card_texts)):
                        for j in range(i + 1, len(_card_texts)):
                            _w1 = _card_texts[i].split()
                            _w2 = _card_texts[j].split()
                            if len(_w1) >= 2 and len(_w2) >= 2:
                                _bg1 = set(zip(_w1[:-1], _w1[1:]))
                                _bg2 = set(zip(_w2[:-1], _w2[1:]))
                                _overlap = len(_bg1 & _bg2) / max(len(_bg1 | _bg2), 1)
                                if _overlap > 0.50:
                                    logger.warning(
                                        "Карты %d и %d дня %d совпадают на %.0f%% биграмм",
                                        i, j, day_index, _overlap * 100,
                                    )
                # Проверка голосовых нарушений: предупреждаем, но не отклоняем
                _violations = _check_violations(str(data.get("text", "")))
                for v in _violations:
                    logger.warning("Голосовое нарушение дня %d: %s", day_index, v)
                # ── Shannon Entropy gate ──
                _chapter_text = str(data.get("text", ""))
                _ent = text_entropy(_chapter_text) if _chapter_text else 0.0
                if len(_chapter_text.split()) > 20:
                    if should_retry_entropy(_chapter_text, attempt):
                        logger.warning(
                            "Низкая энтропия дня %d: %.2f < %.1f (попытка %d) — повтор",
                            day_index, _ent, 3.0, attempt,
                        )
                        continue
                    if _ent > 4.5:
                        logger.info(
                            "Высокая энтропия дня %d: %.2f > %.1f — понижаем температуру",
                            day_index, _ent, 4.5,
                        )
                # ── Kolmogorov Complexity gate ──
                _kol = kolmogorov_ratio(_chapter_text)
                if _kol > 0.75 and len(_chapter_text) > 500:
                    logger.warning(
                        "Текст дня %d бедный (Kolmogorov=%.2f > 0.75, попытка %d) — повтор",
                        day_index, _kol, attempt,
                    )
                    continue
                if _kol < 0.25 and len(_chapter_text) > 500:
                    logger.warning(
                        "Текст дня %d избыточен (Kolmogorov=%.2f < 0.25, попытка %d)",
                        day_index, _kol, attempt,
                    )
                # ── Dynamic temperature: adapt for next attempt ──
                _candidate_texts.append(_chapter_text)
                _current_temp = dynamic_temperature(_current_temp, _ent)
                # ── SA for sealed/important days: optimize params on retry ──
                if sealed and attempt >= 2 and len(_candidate_texts) >= 2:
                    _base = GenerationParams(
                        temperature=_current_temp,
                        max_tokens=settings.llm_max_tokens,
                        frequency_penalty=settings.llm_frequency_penalty,
                        presence_penalty=settings.llm_presence_penalty,
                    )
                    _sa_result = sa_optimize_params(
                        _base, _candidate_texts,
                        rounds=3, seed=f"sa:{day_index}:{attempt}",
                    )
                    _current_temp = _sa_result.temperature
                    logger.info(
                        "SA для sealed дня %d: temp=%.2f → %.2f (попытка %d)",
                        day_index, settings.llm_temperature, _current_temp, attempt,
                    )
                logger.info("Глава дня сгенерирована моделью %s (попытка %d)", used_model, attempt)
                return data
            logger.warning("Модель %s вернула не 3 карты (попытка %d)", used_model, attempt)
        except Exception as exc:
            logger.warning("Глава от %s не разобрана (попытка %d): %s", used_model, attempt, exc)
    return None


async def generate_epilogue(
    day_index: int,
    winner_title: str,
    winner_consequence: str,
    counts_line: str,
    rule_phrase: str,
    season_note: str | None = None,
) -> str:
    """Эпилог дня: чем отозвался победивший путь. "" — если сеть молчит."""
    prompt = (
        f"День {day_index} закрылся. Сработал закон дня: {rule_phrase}. "
        f"Победивший путь «{winner_title}»: {winner_consequence} "
        f"Голосование по путям выглядело так (счёт скрыт был до этого момента): {counts_line}. "
        "Напиши завершение истории дня от второго лица на 200-300 знаков: "
        "как этот выбор меняет вечер и что стая почувствует ночью. "
        "Последняя фраза — крючок на завтра: недоговорённый звук, примета или "
        "вопрос без ответа, который завтрашняя глава обязана подхватить. "
        "Без канцелярита. Не пересказывай итоги; только след, который день оставил в мире."
    )
    if season_note:
        prompt += f"\n{season_note}"
    result = await _chat_completion(
        [
            {"role": "system", "content": DM_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        timeout=55,
    )
    if result is None:
        return ""
    payload, used_model = result
    try:
        text = str(payload["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        logger.warning("Эпилог от %s не разобран: %s", used_model, exc)
        return ""
    if not text:
        return ""
    # Потолок согласован с Round.epilogue_text (String(700)).
    text = _clamp_sentence(text, 400)
    # Нижний порог: эпилог просят 200-300 знаков; конспект короче 140 — мусор,
    # пустой срез лучше недочётного, чем дырка в каноне.
    if len(text) < 140:
        logger.warning("Эпилог %d знаков (<140) отброшен", len(text))
        return ""
    if not text_is_clean(text):
        logger.warning("Эпилог отброшен стоп-фильтром")
        return ""
    logger.info("Эпилог дня написан моделью %s", used_model)
    return polish_typography(text)


_TEASER_FALLBACKS = (
    "Урна вскрыта. Архивариус пересчитал голоса, молча завёл папку дня и почему-то дважды перепроверил счёт.",
    "Счёт известен только архиву. Стая чувствует это шерстью: сегодня мир повернётся не туда, куда все смотрели.",
    "Голоса сложились. Хранитель Спорных Версий запечатывает папку — и ухмыляется краем морды.",
    "Всё уже решено, но не названо. Порталы гудят чуть громче обычного: им первым докладывают о переменах.",
    "В архиве сегодня тихо. Слишком тихо для дня, когда счёт сошёлся с первого раза.",
    "Папка дня уже подписана. Чернила ещё не просохли, а мир уже начал перестраиваться.",
    "Безымянная подошла к урне и нюхала её дольше обычного. Собаки знают исход раньше архива.",
    "Администратор заглянул в урну первым. Что он там пересчитал — узнаем вместе с итогами.",
)


async def generate_teaser(day_index: int, rule_phrase: str) -> str:
    """Тизер в час подсчёта: выбор сделан, итог не называется.

    "" — если сеть молчит (тогда scheduler возьмёт офлайн-фолбэк).
    """
    result = await _chat_completion(
        [
            {"role": "system", "content": DM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"День {day_index}: голосование закрыто, идёт подсчёт. Закон дня "
                    f"(игрокам он известен с утра): {rule_phrase}. Победитель и цифры "
                    "СКОРО будут объявлены отдельно — тебе их неизвестно и называть их нельзя. "
                    "Напиши тизер ожидания от третьего лица, 1-2 предложения до 200 знаков: "
                    "архив уже знает результат, стая напряжённо ждёт вскрытия. Драматичная "
                    "недосказанность, лёгкая ирония Архивариуса допустима. Без цифр, без "
                    "победителя, без обращений к игроку, чистый текст без JSON."
                ),
            },
        ],
        timeout=45,
    )
    if result is None:
        return ""
    payload, used_model = result
    try:
        text = str(payload["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        logger.warning("Тизер от %s не разобран: %s", used_model, exc)
        return ""
    if not text or not text_is_clean(text):
        logger.warning("Тизер отброшен (пустой или нечистый)")
        return ""
    text = _clamp_sentence(text, 320)
    # Тизер — 1-2 фразы; короче 60 это обрубок без недосказанности.
    if len(text) < 60:
        logger.warning("Тизер %d знаков (<60) отброшен", len(text))
        return ""
    logger.info("Тизер подсчёта написан моделью %s", used_model)
    return polish_typography(text)


_REPAIR_CLOSES = (1, 2, 3)


def _clamp_sentence(text: str, limit: int) -> str:
    """Обрезает текст по потолку, режа по последнему знаку в границе.

    Иначе — без знака в границе — режет жёстко и добавляет многоточие.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("…"))
    return cut[: stop + 1] if stop > 0 else cut.rstrip(" ,;:-") + "…"


def _extract_json(content: str) -> dict:
    """Вытаскивает первый корректный JSON-объект из ответа модели.

    raw_decode парсит по токенам и знает про кавычки/экранирование, поэтому в
    отличие от наивного среза по первому/последнему {..} он переживает:
      - текст до/после JSON («Вот ваш JSON: …» -> разбирает);
      - фигурные скобки ВНУТРИ строковых значений (rfind('}') их бы ломал);
      - незакрытый JSON (finish_reason=length при max_tokens): докручивает
        недостающие скобки, а не выходит сразу в офлайн.
    Не разобралось — ValueError, вызывающий код повторит попытку или уйдёт в
    офлайн-версию.
    """
    text = content.strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("JSON не найден в ответе модели")
    body = text[start:]
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(body, 0)
        if isinstance(obj, dict):
            return obj
        raise ValueError("JSON не является объектом")
    except json.JSONDecodeError:
        # Модель обрезана на токенном лимите и не закрыла структуру: докручиваем
        # недостающие закрывающие скобки по уровню глубины, затем откатываемся к
        # последней корректной границе поля (срез по ",« после строки/}).
        for closes in _REPAIR_CLOSES:
            try:
                obj, _ = decoder.raw_decode(body + "}" * closes, 0)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        for cut_at in _truncation_points(body):
            try:
                obj, _ = decoder.raw_decode(body[:cut_at], 0)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        raise ValueError("JSON в ответе модели не разобран (обрезан?)")


def _truncation_points(body: str):
    """Кандидаты на срез обрезанного JSON от конца к началу — по границам полей.

    Обычный токенный обрез режет после конца очередного поля (за запятой/строкой),
    оставляя хвост незакрытым. Режем по последним «,\n», «},», «}», «],» от конца к
    началу, чтобы вытащить максимально полный JSON. Не пытаемся резать внутри
    строк: raw_decode сам отвергнет битый срез.
    """
    seen: list[int] = []
    i = len(body)
    for token in (",\n", ",\r\n", "},\n", "],\n", "}\n"):
        pos = body.rfind(token, 0, i)
        while pos != -1 and len(seen) < 24:
            seen.append(pos + len(token))
            pos = body.rfind(token, 0, pos)
    # уникальные, убывающие от конца к началу
    yield from sorted({p for p in seen if p > 0}, reverse=True)
