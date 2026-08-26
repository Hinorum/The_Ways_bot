"""Арт-директор дня: один кадр вместо четырёх генераций.

Инцидент-предыстория: залп из четырёх запросов в минуту по free-провайдерам
регулярно упирался в 429/таймауты — 1-2 картинки получались, остальные уходили
в PIL-заглушки. Решение — «один кадр дня»: обложка, которая показывает МИР
ПОСЛЕ ВЧЕРАШНЕГО ВЫБОРА (последствие канона как ядро композиции), плюс
стартовый кадр мира один раз на забег. Пути голосования остаются текстом
и кнопками: смысл выбора читается словами лучше, чем нейрокартинкой.

1) «Режиссёр» — LLM-запрос строит визуальную библию дня: палитра, свет,
   сквозные мотивы и ОДИН кадр. Ответ валидируется; при молчании сети
   работает детерминированный офлайн-план.
2) «Промпт-инженер» — чистая функция собирает финальный английский промпт.
3) Лестница генерации в story.fetch_day_image: Gemini («nano banana») →
   Pollinations → детерминированный PIL-фолбэк без текста.
"""

from __future__ import annotations

import hashlib
import logging

from app.config import settings
from app.story import _chat_completion, _extract_json, styled_prompt, text_is_clean

logger = logging.getLogger(__name__)

ART_SYSTEM_PROMPT = (
    "Ты — арт-директор визуальной новеллы по мотивам тёмной сказки о стае "
    "бездомных собак-путешественников у цифровых порталов. Мир: Эхо Стаи. "
    + settings.world_brief
    + " Твоя задача — придумать визуальный язык ОДНОГО игрового дня: "
    "единственный широкий кинематографичный кадр, который показывает мир "
    "ПОСЛЕ вчерашнего выбора стаи. Последствие канонического события — ядро "
    "композиции: то, что стая построила, сломала или разбудила вчера, видно "
    "в сцене сегодня. Ты не пишешь текст для игрока и не упоминаешь механику "
    "игры: только изображение."
)

_NEGATIVE_SUFFIX = (
    ", no text, no letters, no numbers, no typography, no watermark, no logo, "
    "no poster layout, no frames, no borders"
)

# Вариативные приёмы: вращаются от сида, чтобы даже похожие дни смотрелись иначе.
_COVER_COMPOSITIONS = [
    "extreme wide establishing shot with deep perspective",
    "sweeping aerial view of the scene",
    "low horizon panorama under a vast sky",
    "symmetrical wide shot centered on a glowing portal",
]
_CARD_COMPOSITIONS = [
    "low angle hero shot",
    "over-the-shoulder view",
    "dutch angle close-up",
    "top-down view of a small figure",
]
_CAMERA_TEXTURES = [
    "35mm film grain, cinematic still",
    "matte painting detail, soft depth of field",
    "ink-and-watercolor accents over digital painting",
    "anamorphic lens flare, subtle chromatic fringe",
]

_PALETTE_ROTATION = [
    ("muted teal and ember orange with deep violet shadows", "volumetric god rays through fog"),
    ("cold slate blue with amber lantern light", "cold moonlit rim light"),
    ("dusty rose and rusted copper with teal dusk", "warm ember glow"),
    ("abyssal green-black with bioluminescent turquoise", "bioluminescent haze"),
]


# Междинная преемственность: якорь предыдущего дня (палитра/свет/мотивы)
# протаскивается в библию следующего — сериальность вместо лотереи.
AnchorKey = "art_anchor"

# Стабильные визуальные дескрипторы постоянных лиц мира: кто упомянут в главе —
# тот и получает узнаваемую фигуру в кадре. Детерминированно, без сети и БД:
# Лайнер в кадре сегодня и через неделю — одна и та же силуэтность.
_CHARACTER_MOTIFS = {
    "лайнер": (
        "Liner the memory-trader, hooded stray dog with soft lantern eyes "
        "and a satchel of glowing memory vials"
    ),
    "архивариус": (
        "Archivist Keeper of Contested Versions, gaunt dog shrouded in "
        "drifting paper dust, thin spectacles, whispering folders"
    ),
    "хранитель спорных версий": (
        "Archivist Keeper of Contested Versions, gaunt dog shrouded in "
        "drifting paper dust, thin spectacles, whispering folders"
    ),
    "хозяин ошибки": (
        "faceless Error Master, a tall silhouette of counting hands and "
        "misplaced numbers hovering over a glitching portal"
    ),
    # НейроГримёр Еретика: ветеран носит старый мир на себе — пальто из
    # выцветших карт старой Стаи; знак присвоения (апостроф) читается даже
    # в тени кадра; правила при нём как ошейник — он и закон в одном лице.
    "еретик": (
        "Heretic the Way-Leaver, lean wiry scarred stray dog in a patchwork "
        "coat stitched from faded old maps, chalk-white apostrophe-shaped "
        "mark over one narrowed eye, small slate rule-tags braided into his "
        "collar"
    ),
    "свернувший с пути": (
        "Heretic the Way-Leaver, lean wiry scarred stray dog in a patchwork "
        "coat stitched from faded old maps, chalk-white apostrophe-shaped "
        "mark over one narrowed eye, small slate rule-tags braided into his "
        "collar"
    ),
}


def character_motifs_for(text: str) -> list[str]:
    """Дескрипторы персонажей, упомянутых в тексте главы (без дублей)."""
    low = (text or "").lower()
    return sorted({descriptor for needle, descriptor in _CHARACTER_MOTIFS.items() if needle in low})


def offline_bible(chapter: dict, anchor: dict | None = None) -> dict:
    """Детерминированный план дня без сети: один кадр из cover_prompt главы.

    anchor — компактный якорь предыдущего дня: палитра продолжает ротацию
    с него (а не с нуля), чтобы офлайн-дни тоже шли «серией».
    """
    day_key = str(chapter.get("title", "")) or "day"
    seed = int(hashlib.sha256(day_key.encode("utf-8")).hexdigest()[:8], 16)
    palettes = [pair[0] for pair in _PALETTE_ROTATION]
    try:
        base_idx = palettes.index(str((anchor or {}).get("palette", "")))
        palette_idx = (base_idx + 1) % len(_PALETTE_ROTATION)
    except ValueError:
        palette_idx = seed % len(_PALETTE_ROTATION)
    palette, lighting = _PALETTE_ROTATION[palette_idx]
    shots: dict[str, dict[str, str]] = {
        "cover": {
            "scene": chapter.get(
                "cover_prompt",
                "pack of stray dogs before an unstable glowing portal",
            ),
            "composition": _COVER_COMPOSITIONS[seed % len(_COVER_COMPOSITIONS)],
        }
    }
    return {
        "palette": palette,
        "lighting": lighting,
        "motifs": ["glowing portal ring", "drifting digital particles"],
        "shots": shots,
    }


def _build_art_prompt(chapter: dict, recent_beats: list[str], anchor: dict | None = None) -> str:
    last_beat = recent_beats[-1] if recent_beats else ""
    yesterday_block = (
        f"\nЯДРО КАДРА — чем обернулся вчерашний выбор стаи: «{last_beat}». "
        "Покажи его последствие в сцене (постройка или руина, след или примета, "
        "изменившееся место), не иллюстрируя событие буквально.\n"
        if last_beat
        else "\nКанона вчера нет: покажи стаю в момент прихода в новый мир.\n"
    )
    anchor_block = ""
    if anchor and anchor.get("palette"):
        motifs = ", ".join(anchor.get("motifs") or [])
        anchor_block = (
            f"\nПРЕДЫДУЩИЙ ДЕНЬ: палитра «{anchor.get('palette', '')}», свет "
            f"«{anchor.get('lighting', '')}», мотивы: {motifs}. Сохрани узнаваемость "
            "стиля (та же гамма и сквозные мотивы), но полностью смени локацию "
            "и ракурс — новый день не должен выглядеть копией вчерашнего.\n"
        )
    return (
        "Ответь только JSON, все текстовые значения на английском. Собери "
        "визуальную библию одного дня игры: ОДИН кадр.\n\n"
        f"ГЛАВА ДНЯ: «{chapter.get('title', '')}»\n{chapter.get('text', '')[:700]}\n"
        f"{yesterday_block}"
        f"{anchor_block}"
        "Требования. Обложка — широкий кинематографичный кадр всей сцены дня; "
        "последствие вчерашнего канона читается в сцене первым взглядом. Стая "
        "присутствует в кадре; если в тексте есть Еретик — это тощий пёс в "
        "пальто из старых карт с белым знаком-апострофом у глаза. На изображении "
        "не должно быть никакого текста. Не используй слова vote, card, player, UI.\n"
        'Формат: {"palette":"english color palette phrase","lighting":"english '
        'lighting phrase","motifs":["english motif","english motif"],'
        '"shots":{"cover":{"scene":"...","composition":"..."}}}'
    )


def _parse_bible(payload: dict) -> dict | None:
    content = payload["choices"][0]["message"]["content"]
    data = _extract_json(content)
    shots_raw = data.get("shots")
    if not isinstance(shots_raw, dict):
        return None
    shot = shots_raw.get("cover")
    if not isinstance(shot, dict):
        return None
    scene = str(shot.get("scene", "")).strip()
    composition = str(shot.get("composition", "")).strip()
    if not scene or not composition:
        return None
    shots = {"cover": {"scene": scene[:400], "composition": composition[:200]}}
    palette = str(data.get("palette", "")).strip() or _PALETTE_ROTATION[0][0]
    lighting = str(data.get("lighting", "")).strip() or _PALETTE_ROTATION[0][1]
    motifs = [str(m).strip()[:80] for m in (data.get("motifs") or []) if str(m).strip()][:3]
    if not motifs:
        motifs = ["glowing portal ring"]
    blob = " ".join([palette, lighting, *motifs, scene])
    if not text_is_clean(blob):
        logger.warning("Библия дня отброшена стоп-фильтром")
        return None
    return {"palette": palette[:200], "lighting": lighting[:200], "motifs": motifs, "shots": shots}


def _merge_motifs(bible: dict, extra_motifs: list[str] | None) -> dict:
    """Визуальные следы эхов: предмет из давнего дня попадает в кадр."""
    if extra_motifs and isinstance(bible.get("motifs"), list):
        for motif in extra_motifs:
            if motif not in bible["motifs"]:
                bible["motifs"].append(motif)
    return bible


async def plan_day_art(
    chapter: dict,
    recent_beats: list[str] | None = None,
    anchor: dict | None = None,
    extra_motifs: list[str] | None = None,
) -> dict:
    """Библия дня: LLM-план с одной повторной попыткой, иначе офлайн-план.

    anchor — якорь предыдущего дня для преемственности стиля; extra_motifs —
    визуальные приметы всплывших эхов (предмет давнего дня в кадре).
    """
    beats = recent_beats or []
    if not settings.use_free_story_llm:
        return _merge_motifs(offline_bible(chapter, anchor=anchor), extra_motifs)
    messages = [
        {"role": "system", "content": ART_SYSTEM_PROMPT},
        {"role": "user", "content": _build_art_prompt(chapter, beats, anchor)},
    ]
    for attempt in range(1, 3):
        result = await _chat_completion(messages)
        if result is None:
            break
        payload, used_model = result
        try:
            bible = _parse_bible(payload)
        except Exception as exc:
            logger.warning("Библия от %s не разобрана (попытка %d): %s", used_model, attempt, exc)
            continue
        if bible is not None:
            logger.info("Арт-библия дня составлена моделью %s (попытка %d)", used_model, attempt)
            return _merge_motifs(bible, extra_motifs)
        logger.warning("Модель %s вернула неполную библию (попытка %d)", used_model, attempt)
    # Якорь передаётся и фолбэку: палитра не должна теряться при морге сети.
    return _merge_motifs(offline_bible(chapter, anchor=anchor), extra_motifs)


def compact_anchor(bible: dict) -> dict:
    """Компактный якорь для хранения в watcher_state (лимит 255 символов)."""
    return {
        "palette": str(bible.get("palette", ""))[:60],
        "lighting": str(bible.get("lighting", ""))[:60],
        "motifs": [str(m)[:40] for m in (bible.get("motifs") or [])][:2],
    }


def build_image_prompt(bible: dict, slot: str, seed: int = 0) -> str:
    """Финальный промпт кадра из библии. Чистая функция — покрывается тестами."""
    shot = bible.get("shots", {}).get(slot) or next(iter(bible.get("shots", {}).values()))
    pool = _COVER_COMPOSITIONS if slot == "cover" else _CARD_COMPOSITIONS
    composition = shot.get("composition") or pool[seed % len(pool)]
    texture = _CAMERA_TEXTURES[seed % len(_CAMERA_TEXTURES)]
    motif = ""
    motifs = bible.get("motifs") or []
    if motifs:
        motif = ", " + motifs[seed % len(motifs)]
    prompt = (
        f"{shot['scene']}, {composition}, {bible.get('palette', '')}, "
        f"{bible.get('lighting', '')}{motif}, {texture}{_NEGATIVE_SUFFIX}"
    )
    return styled_prompt(prompt)


def short_image_prompt(bible: dict, slot: str, seed: int = 0) -> str:
    """Сжатый запасной промпт: только суть сцены — длинные промпты иногда давят."""
    shot = bible.get("shots", {}).get(slot) or next(iter(bible.get("shots", {}).values()))
    words = " ".join(shot["scene"].split()[:28])
    return styled_prompt(f"{words}{_NEGATIVE_SUFFIX}")


# Стартовый кадр забега: мир целиком, для знакомства игроков. Генерируется
# один раз на забег (день 1), дальше переиспользуется файлом.
_INTRO_SCENE = (
    "sweeping establishing shot of the pack's new world: a valley of glitching "
    "portal rings under a vast dusk sky, five stray dog silhouettes on a ridge "
    "looking down, a faint faraway light of the First Bark deep below the "
    "network, a lean scarred dog in a coat of old stitched maps watching from "
    "a near cliff edge"
)


def build_intro_prompt(bible: dict, seed: int = 0) -> str:
    """Промпт стартового кадра мира: палитра и мотивы дня + константа сцены."""
    texture = _CAMERA_TEXTURES[seed % len(_CAMERA_TEXTURES)]
    motifs = bible.get("motifs") or []
    motif = f", {motifs[seed % len(motifs)]}" if motifs else ""
    prompt = (
        f"{_INTRO_SCENE}, extreme wide establishing shot, {bible.get('palette', '')}, "
        f"{bible.get('lighting', '')}{motif}, {texture}{_NEGATIVE_SUFFIX}"
    )
    return styled_prompt(prompt)


def build_intro_short_prompt(bible: dict) -> str:
    # styled_prompt сам добавляет стиль и запреты текста — без дублей.
    return styled_prompt(_INTRO_SCENE)
