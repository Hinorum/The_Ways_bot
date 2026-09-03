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

import asyncio
import hashlib
import logging
import re

from app.config import settings
from app.story import _chat_completion, _extract_json, styled_prompt, text_is_clean

logger = logging.getLogger(__name__)

ART_SYSTEM_PROMPT = (
    "Ты — арт-директор визуальной новеллы по мотивам тёмной сказки о стае "
    "бездомных собак-путешественников у стен лабиринта. Мир: Эхо Стаи. "
    "ВИЗУАЛЬНЫЙ СТИЛЬ СЕРИИ — flat 2D vector cartoon, cozy-dystopia: смелые "
    "чистые контуры, приглушённые матовые цвета, никакой фотореалистичности, "
    "3D-рендера или аниме. Кадр читается как иллюстрация к сказке, а не как "
    "живое фото. "
    + settings.world_brief
    + " Твоя задача — придумать визуальный язык ОДНОГО игрового дня: "
    "единственный широкий кинематографичный кадр, который показывает мир "
    "ПОСЛЕ вчерашнего выбора стаи. Последствие канонического события — ядро "
    "композиции: то, что стая построила, сломала или разбудила вчера, видно "
    "в сцене сегодня. Ты не пишешь текст для игрока и не упоминаешь механику "
    "игры: только изображение.\n"
    "ЦВЕТОВАЯ СЕМАНТИКА СЕРИИ — держи изо дня в день, палитра через сюжет: "
    "красное свечение — только приметы Администратора и чужого счёта; "
    "мелково-белая отметка в форме апострофа — знак Еретика; "
    "биолюминесцентная бирюза — коридоры и стены лабиринта; "
    "тёплое золото — миски, чудо и память стаи; "
    "пыльно-серый — страницы старого дневника; "
    "тёмно-коричневый и грязно-оранжевый — подвал, Крыса и обглоданные таблички; "
    "зеленовато-золотой и тени длиннее обычного — предвестия Анубиса и чаши весов."
)

_NEGATIVE_SUFFIX = (
    ", no text, no letters, no numbers, no typography, no watermark, no logo, "
    "no poster layout, no frames, no borders"
    # Антидрейф стиля (уроки промпт-пака): free-модели уползают в фотореализм,
    # 3D-рендер или аниме-скрин без явных запретов; руки/лишние конечности —
    # типовые артефакты существ в кадре. Серия — flat 2D vector cozy-dystopia,
    # поэтому запрещаем и «живописные» текстуры.
    ", photorealistic, 3D render, realistic fur, anime screencap, glossy render, "
    "digital painting, oil painting, painterly texture, watercolor wash, "
    "human hands, extra limbs, deformed face, cluttered background"
)

# Вариативные приёмы: вращаются от сида, чтобы даже похожие дни смотрелись иначе.
_COVER_COMPOSITIONS = [
    "extreme wide establishing shot with deep perspective",
    "sweeping aerial view of the scene",
    "low horizon panorama under a vast sky",
    "symmetrical wide shot centered on a labyrinth corridor",
]
_CARD_COMPOSITIONS = [
    "low angle hero shot",
    "over-the-shoulder view",
    "dutch angle close-up",
    "top-down view of a small figure",
]
_CAMERA_TEXTURES = [
    "flat 2D vector illustration, bold clean outlines, matte colors",
    "limited flat color palette, hard-edged shapes, storybook illustration",
    "bold contour lines, no gradients, cozy-dystopia paper-cutout feel",
    "flat graphic novel panel, muted matte finish, subtle grain",
]

# Гамма серии (cozy-dystopia, flat-vector): песочно-бежевый / жжёно-оранжевый /
# пыльно-бирюзовый / угольный — база; неоново-радиоактивно-красный / монетно-
# золотой / кислотно-зелёный — акценты. Цветовая семантика (см. ART_SYSTEM_PROMPT)
# не меняется: красное — Администратор, бирюза — коридоры, золото — миски/память.
_PALETTE_ROTATION = [
    ("sandy beige and burnt orange with dusty teal shadows, charcoal linework", "soft hazy dusk glow"),
    ("dusty teal and charcoal with coin-gold lantern light", "cold moonlit rim light"),
    ("burnt orange and sandy beige with acid-green accents", "warm ember glow"),
    ("charcoal and dusty teal with neon radioactive-red glints", "bioluminescent haze"),
    ("dark slate and blood-red with cold steel highlights", "harsh industrial glare"),
    ("deep navy and silver with warm amber accents", "candlelit warmth"),
    ("muted olive and rust with pale yellow undertones", "overcast diffused light"),
    ("obsidian black and forest green with mossy highlights", "undergrowth filtered light"),
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
        "and a satchel of glowing memory vials, an old dusty silent radio "
        "slung on a strap at his hip"
    ),
    "дневник": (
        "an old weathered journal with faded pages, glowing faintly in the dark, "
        "pages turning by themselves, whispering secrets of the labyrinth"
    ),
    "администратор": (
        "faceless Administrator, a tall silhouette of counting hands and "
        "misplaced numbers hovering over glitching labyrinth walls, surrounded by "
        "perfectly aligned but sad, sterile tableaus"
    ),
    # НейроГримёр Еретика: ветеран носит старый мир на себе — пальто из
    # выцветших карт старой Стаи; знак присвоения (апостроф) читается даже
    # в тени кадра; под пальто спрятан выцветший ошейник старой Стаи;
    # правила при нём как ошейник — он и закон в одном лице.
    "еретик": (
        "Heretic the Way-Leaver, lean wiry scarred stray dog in a patchwork "
        "coat stitched from faded old maps, chalk-white apostrophe-shaped "
        "mark over one narrowed eye, small slate rule-tags braided into his "
        "collar, a faded old-Pack collar hidden beneath the coat"
    ),
    "свернувший с пути": (
        "Heretic the Way-Leaver, lean wiry scarred stray dog in a patchwork "
        "coat stitched from faded old maps, chalk-white apostrophe-shaped "
        "mark over one narrowed eye, small slate rule-tags braided into his "
        "collar, a faded old-Pack collar hidden beneath the coat"
    ),
    # Пятёрка псов: у каждой свой видимый «почерк», чтобы в кадре читались
    # отдельными фигурами, а не общим силуэтом стаи.
    "баркод": (
        "Barcode the counting mutt, charcoal dog in a striped scarf woven "
        "from a calendar, focused intently counting passers-by"
    ),
    "стежка": (
        "Stezhka the early-hearer, ash-grey dog with a patch on one ear, "
        "always a step ahead of the pack, ear tilted to a faint sound"
    ),
    "вектор": (
        "Vector the stubborn one, straight-backed brindle dog standing "
        "rigid against the wind, muzzle aimed dead ahead"
    ),
    "пиксель": (
        "Pixel the spark-catcher, small speckled dog with a faint glowing "
        "digital spark held between his teeth"
    ),
    "безымянная": (
        "Nameless, the dog the old game could never count — a pale dog with "
        "an empty collar, gazing where no one else looks"
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
                "pack of stray dogs before labyrinth corridors",
            ),
            "composition": _COVER_COMPOSITIONS[seed % len(_COVER_COMPOSITIONS)],
        }
    }
    return {
        "palette": palette,
        "lighting": lighting,
        # Эмоциональное ядро серии — «круг света стаи» (аналог их костра
        # под звёздами): тёплый островок среди глючных миров.
        "motifs": [
            "glowing corridor walls",
            "drifting digital particles",
            "a small circle of campfire light around the pack under a starry sky",
        ],
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
    # Инжектим визуальные дескрипторы персонажей, упомянутых в главе
    chapter_text = f"{chapter.get('title', '')} {chapter.get('text', '')}"
    char_motifs = character_motifs_for(chapter_text)
    # Добавляем NPC из AI World Engine
    ai_chars = chapter.get("ai_characters", [])
    for npc in ai_chars:
        name = npc.get("name", "")
        if name and name.lower() not in chapter_text.lower():
            continue
        mood = npc.get("mood", "neutral")
        trust = npc.get("trust", 5)
        # Генерируем визуальный дескриптор на основе mood/trust
        if mood == "hostile":
            visual = f"{name}, tense aggressive silhouette, ears pinned back, bared teeth"
        elif mood == "friendly":
            visual = f"{name}, relaxed warm figure, soft eyes, open posture"
        elif mood == "fearful":
            visual = f"{name}, crouching fearful shape, tucked tail, wide eyes"
        elif mood == "sad":
            visual = f"{name}, drooping weary figure, lowered head, dull eyes"
        else:
            visual = f"{name}, neutral calm figure, alert posture"
        # Модификатор по trust
        if trust <= 3:
            visual += ", distrustful glance, keeping distance"
        elif trust >= 8:
            visual += ", loyal companion, close to the pack"
        char_motifs.append(visual)
    char_block = ""
    if char_motifs:
        char_block = "\nПЕРСОНАЖИ В КАДРЕ (добавь описанных фигур): " + "; ".join(char_motifs) + ".\n"
    # Атмосфера локации из AI World Engine
    atmosphere = chapter.get("atmosphere", "")
    atmosphere_block = ""
    if atmosphere:
        atmosphere_block = f"\nАТМОСФЕРА ЛОКАЦИИ: {atmosphere}\n"
    # Сцена локации из AI World Engine (английский промпт)
    location_scene = chapter.get("location_scene", "")
    scene_override = ""
    if location_scene:
        scene_override = f"\nСЦЕНА ЛОКАЦИИ (испуй как основу): {location_scene}\n"
    return (
        "Ответь только JSON, все текстовые значения на английском. Собери "
        "визуальную библию одного дня игры: ОДИН кадр.\n\n"
        f"ГЛАВА ДНЯ: «{chapter.get('title', '')}»\n{chapter.get('text', '')[:700]}\n"
        f"{yesterday_block}"
        f"{anchor_block}"
        f"{char_block}"
        f"{atmosphere_block}"
        f"{scene_override}"
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
        motifs = ["glowing corridor walls"]
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
    recent_prompts: list[str] | None = None,
) -> dict:
    """Библия дня: LLM-план с одной повторной попыткой, иначе офлайн-план.

    anchor — якорь предыдущего дня для преемственности стиля; extra_motifs —
    визуальные приметы всплывших эхов (предмет давнего дня в кадре).
    recent_prompts — последние промпты обложек для dedup.
    """
    beats = recent_beats or []
    if not settings.use_free_story_llm:
        return _merge_motifs(offline_bible(chapter, anchor=anchor), extra_motifs)
    messages = [
        {"role": "system", "content": ART_SYSTEM_PROMPT},
        {"role": "user", "content": _build_art_prompt(chapter, beats, anchor)},
    ]
    # Сетевой сбой уже ретраится внутри _chat_completion; здесь — повторный
    # заход на случай, если модель очнулась/сменилась между попытками. Так же,
    # как у генератора главы (_free_story_llm), — без асимметрии.
    for attempt in range(1, 4):  # 3 попытки с учётом dedup
        result = await _chat_completion(messages, temperature=0.6, max_tokens=2000)
        if result is None:
            if attempt < 3:
                logger.warning("Арт-библия: все модели недоступны — повтор через 3 с")
                await asyncio.sleep(3)
                continue
            break
        payload, used_model = result
        try:
            bible = _parse_bible(payload)
        except Exception as exc:
            logger.warning("Библия от %s не разобрана (попытка %d): %s", used_model, attempt, exc)
            continue
        if bible is not None:
            # Проверяем dedup
            if recent_prompts:
                test_prompt = build_image_prompt(bible, "cover", seed=attempt)
                if check_prompt_dedup(test_prompt, recent_prompts):
                    logger.warning("Библия от %s дублирует обложку (попытка %d)", used_model, attempt)
                    continue
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
    "sweeping establishing shot of the pack's new world: a valley of labyrinth "
    "corridors under a vast dusk sky, five stray dog silhouettes on a ridge "
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


# ── Dedup: проверка на повтор промптов ──────────────────────────────────────

def _normalize_prompt_for_dedup(prompt: str) -> str:
    """Нормализует промпт для сравнения: убирает стоп-слова, приводит к нижнему регистру."""
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
        "neither", "each", "every", "all", "any", "few", "more", "most", "other",
        "some", "such", "no", "only", "own", "same", "than", "too", "very",
        "just", "because", "if", "when", "while", "where", "how", "what",
        "which", "who", "whom", "this", "that", "these", "those",
        # Русские стоп-слова
        "и", "а", "но", "что", "как", "где", "когда", "если", "или", "ни",
        "не", "нет", "да", "то", "в", "на", "с", "из", "за", "по", "для",
        "от", "до", "при", "без", "под", "над", "между", "через", "после",
        "перед", "около", "рядом", "вместо", "кроме", "except",
    }
    words = re.findall(r'\w+', prompt.lower())
    return " ".join(w for w in words if w not in stop_words and len(w) > 2)


def check_prompt_dedup(
    new_prompt: str,
    recent_prompts: list[str],
    threshold: float = 0.6,
) -> bool:
    """Проверяет, не дублирует ли новый промпт последние обложки.

    Args:
        new_prompt: новый промпт для проверки
        recent_prompts: список последних промптов (последний = самый свежий)
        threshold: порог схожести (0-1, где 1 = идентичны)

    Returns:
        True если промпт слишком похож (дубликат), False если ОК.
    """
    if not recent_prompts:
        return False

    norm_new = _normalize_prompt_for_dedup(new_prompt)
    if not norm_new:
        return False

    words_new = set(norm_new.split())
    if not words_new:
        return False

    for old_prompt in recent_prompts[-3:]:  # Проверяем последние 3 обложки
        norm_old = _normalize_prompt_for_dedup(old_prompt)
        words_old = set(norm_old.split())
        if not words_old:
            continue

        # Jaccard similarity
        intersection = len(words_new & words_old)
        union = len(words_new | words_old)
        if union == 0:
            continue

        similarity = intersection / union
        if similarity >= threshold:
            logger.warning(
                "Промпт дублирует недавнюю обложку (similarity=%.2f): "
                "новый=%s... старый=%s...",
                similarity,
                new_prompt[:80],
                old_prompt[:80],
            )
            return True

    return False


async def get_recent_image_prompts(
    session: "AsyncSession",
    limit: int = 3,
) -> list[str]:
    """Загружает последние N промптов обложек из БД."""
    from sqlalchemy import select as sa_select
    from app.models import Round

    q = (
        sa_select(Round.cover_image_prompt)
        .where(Round.cover_image_prompt.isnot(None))
        .order_by(Round.day_index.desc())
        .limit(limit)
    )
    result = await session.execute(q)
    return [row[0] for row in result.all() if row[0]]


# ── Quality Gate: Laplacian variance ────────────────────────────────────────

def calculate_laplacian_variance(image_path: str | Path) -> float:
    """Вычисляет дисперсию Лапласиана для проверки резкости изображения.

    Низкое значение = размытое/пустое изображение.
    Высокое значение = чёткое/детализированное изображение.
    Использует только PIL — без numpy.

    Returns:
        Дисперсия Лапласиана (обычно от 0 до 2000+).
    """
    from PIL import Image, ImageFilter

    img = Image.open(image_path).convert("L")  # Grayscale
    # Лапласиан через встроенный фильтр PIL
    laplacian = img.filter(ImageFilter.Kernel(
        size=(3, 3),
        kernel=[0, 1, 0, 1, -4, 1, 0, 1, 0],
        scale=1,
        offset=0,
    ))
    pixels = list(laplacian.getdata())
    n = len(pixels)
    if n == 0:
        return 0.0
    mean = sum(pixels) / n
    variance = sum((p - mean) ** 2 for p in pixels) / n
    return float(variance)


def check_image_quality(
    image_path: str | Path,
    min_variance: float = 100.0,
) -> tuple[bool, float]:
    """Проверяет качество изображения по дисперсии Лапласиана.

    Args:
        image_path: путь к изображению
        min_variance: минимальная допустимая дисперсия

    Returns:
        (passed, variance) — прошло ли проверку и 실제 дисперсия
    """
    try:
        variance = calculate_laplacian_variance(image_path)
        passed = variance >= min_variance
        if not passed:
            logger.warning(
                "Изображение не прошло quality gate: variance=%.2f < %.2f (%s)",
                variance,
                min_variance,
                image_path,
            )
        return passed, variance
    except Exception as exc:
        logger.warning("Не удалось проверить качество изображения %s: %s", image_path, exc)
        return False, 0.0


async def fetch_image_with_quality_check(
    prompt: str,
    short_prompt: str,
    dest: Path,
    seed: int | None = None,
    width: int = 768,
    height: int = 1024,
    negative_prompt: str | None = None,
    min_variance: float = 100.0,
    max_retries: int = 2,
) -> tuple[bool, float]:
    """Генерирует изображение с проверкой качества.

    Если изображение не проходит quality gate, повторяет попытку.
    Возвращает (success, final_variance).
    """
    from app.story import fetch_day_image

    for attempt in range(max_retries):
        success = await fetch_day_image(
            prompt=prompt,
            short_prompt=short_prompt,
            dest=dest,
            seed=seed,
            width=width,
            height=height,
            negative_prompt=negative_prompt,
        )
        if not success:
            continue

        passed, variance = check_image_quality(dest, min_variance)
        if passed:
            return True, variance

        # Повторяем с другим seed
        if seed is not None:
            seed = seed + 1_000_000

    return False, 0.0
