"""Арт-директор дня: цепочка «замысел → промпт → генерация» вместо одного
сырого image_prompt из главы.

1) «Режиссёр» — отдельный LLM-запрос строит визуальную библию дня:
   общая палитра, свет, сквозные мотивы и четыре различных кадра (обложка
   и три пути), каждый в своей локации и ракурсе, с привязкой к смыслу
   выбора. Ответ валидируется; при молчании сети работает детерминированный
   офлайн-план из полей главы.
2) «Промпт-инженер» — чистая функция собирает финальный английский промпт
   каждого кадра из библии: сцена, композиция, палитра, свет, мотив плюс
   вариативные кино-приёмы, вращающиеся от сида дня, и жёсткие негативы
   против текста на картинке.
3) Генерация остаётся в story.fetch_free_image (лестница моделей); при
   полном молчании сети rounds рисует абстрактный арт без текста.
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
    + " Твоя задача — придумать визуальный язык одного игрового дня так, чтобы "
    "все картинки дня выглядели как один фильм, но ни одна не повторяла другую "
    "по локации, ракурсу и настроению. Ты не пишешь текст для игрока и не "
    "упоминаешь механику игры: только изображения."
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


def offline_bible(chapter: dict) -> dict:
    """Детерминированный план дня без сети: сцены берём из image_prompt карт."""
    day_key = str(chapter.get("title", "")) or "day"
    seed = int(hashlib.sha256(day_key.encode("utf-8")).hexdigest()[:8], 16)
    palette, lighting = _PALETTE_ROTATION[seed % len(_PALETTE_ROTATION)]
    shots: dict[str, dict[str, str]] = {
        "cover": {
            "scene": chapter.get(
                "cover_prompt",
                "pack of stray dogs before an unstable glowing portal",
            ),
            "composition": _COVER_COMPOSITIONS[seed % len(_COVER_COMPOSITIONS)],
        }
    }
    for position, card in enumerate(chapter.get("cards") or []):
        base = (
            card.get("image_prompt")
            or f"dark fairy-tale tarot scene, {card.get('title', 'stray dog at a crossroads')}"
        )
        shots[str(position)] = {
            "scene": base,
            "composition": _CARD_COMPOSITIONS[(seed + position) % len(_CARD_COMPOSITIONS)],
        }
    while len(shots) < 4:
        shots[str(len(shots))] = {
            "scene": "lone stray dog before a glitching portal",
            "composition": _CARD_COMPOSITIONS[len(shots) % len(_CARD_COMPOSITIONS)],
        }
    return {
        "palette": palette,
        "lighting": lighting,
        "motifs": ["glowing portal ring", "drifting digital particles"],
        "shots": shots,
    }


def _build_art_prompt(chapter: dict, recent_beats: list[str]) -> str:
    cards_block = ""
    for position, card in enumerate(chapter.get("cards") or []):
        cards_block += (
            f"\nПУТЬ {position}: тег {card.get('tag', 'care')}; «{card.get('title', '')}» — "
            f"{card.get('description', '')} Последствие: {card.get('consequence', '')} "
            f"Черновой образ: {card.get('image_prompt', '')}"
        )
    last_beat = recent_beats[-1] if recent_beats else ""
    return (
        "Ответь только JSON, все текстовые значения на английском. Собери "
        "визуальную библию одного дня игры.\n\n"
        f"ГЛАВА ДНЯ: «{chapter.get('title', '')}»\n{chapter.get('text', '')[:700]}\n"
        f"{cards_block}\n"
        f"КАНОН ВЧЕРАШНЕГО ДНЯ: {last_beat}\n\n"
        "Требования. Обложка — широкий кинематографичный кадр всей сцены дня. "
        "Каждый путь — отдельная портретная сцена, передающая СМЫСЛ выбора, а не "
        "буквальную подпись. Четыре кадра должны быть в разных локациях, с разных "
        "ракурсов и с разным настроением, но в единой палитре и свете, с общими "
        "мотивами дня. На изображениях не должно быть никакого текста. "
        "Не используй слова vote, card, player, UI.\n"
        'Формат: {"palette":"english color palette phrase","lighting":"english '
        'lighting phrase","motifs":["english motif","english motif"],'
        '"shots":{"cover":{"scene":"...","composition":"..."},'
        '"0":{"scene":"...","composition":"..."},'
        '"1":{"scene":"...","composition":"..."},'
        '"2":{"scene":"...","composition":"..."}}}'
    )


def _parse_bible(payload: dict) -> dict | None:
    content = payload["choices"][0]["message"]["content"]
    data = _extract_json(content)
    shots_raw = data.get("shots")
    if not isinstance(shots_raw, dict):
        return None
    shots: dict[str, dict[str, str]] = {}
    for slot in ("cover", "0", "1", "2"):
        shot = shots_raw.get(slot)
        if not isinstance(shot, dict):
            return None
        scene = str(shot.get("scene", "")).strip()
        composition = str(shot.get("composition", "")).strip()
        if not scene or not composition:
            return None
        shots[slot] = {"scene": scene[:400], "composition": composition[:200]}
    palette = str(data.get("palette", "")).strip() or _PALETTE_ROTATION[0][0]
    lighting = str(data.get("lighting", "")).strip() or _PALETTE_ROTATION[0][1]
    motifs = [str(m).strip()[:80] for m in (data.get("motifs") or []) if str(m).strip()][:3]
    if not motifs:
        motifs = ["glowing portal ring"]
    blob = " ".join([palette, lighting, *motifs, *(s["scene"] for s in shots.values())])
    if not text_is_clean(blob):
        logger.warning("Библия дня отброшена стоп-фильтром")
        return None
    return {"palette": palette[:200], "lighting": lighting[:200], "motifs": motifs, "shots": shots}


async def plan_day_art(chapter: dict, recent_beats: list[str] | None = None) -> dict:
    """Библия дня: LLM-план с одной повторной попыткой, иначе офлайн-план."""
    beats = recent_beats or []
    if not settings.use_free_story_llm:
        return offline_bible(chapter)
    messages = [
        {"role": "system", "content": ART_SYSTEM_PROMPT},
        {"role": "user", "content": _build_art_prompt(chapter, beats)},
    ]
    for attempt in range(1, 3):
        result = await _chat_completion(messages, timeout=40)
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
            return bible
        logger.warning("Модель %s вернула неполную библию (попытка %d)", used_model, attempt)
    return offline_bible(chapter)


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
