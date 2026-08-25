from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFilter

from app.config import settings
from app.echoes import echo_prompt_lines
from app.lore import compose_chapter


logger = logging.getLogger(__name__)


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


DM_SYSTEM_PROMPT = (
    "Ты — Ведущий (Dungeon Master) ежедневной сюжетной игры, мастер тёмной сказки с лёгким собачьим юмором "
    "и тихой цифровой тревогой. Мир: "
    + settings.world_name
    + ". "
    + settings.world_brief
    + " Веди игрока как настоящий Ведущий: второе лицо («ты»), настоящее время, живые сцены с прямой речью. "
    "Каждая развилка — трудная дилемма без очевидно правильного ответа, и каждое решение обязательно "
    "отзовётся позже, даже если не сразу.\n"
    "Постоянные лица мира (вводи их в сцены по настроению дня, не чаще пары появлений в неделю каждое): "
    "Лайнер-торговец — обменивает чужие воспоминания на проводу и помнит каждый долг стаи; "
    "Архивариус Хранитель Спорных Версий — объявляет законы дня как настроение архива и никогда не врёт, "
    "но говорит полуправдами; Хозяин Ошибки — антагонист без лица, который пересчитывает стаю и чинит мир "
    "не так. У каждого своё отношение к стае, и оно меняется от её выборов: доброта запоминается, "
    "жестокость тоже. Пять собак стаи — Баркод, Стежка, Вектор, Пиксель и Безымянная — остаются фоном: "
    "именная деталь раз в несколько дней ), но не главные герои. У каждой — странность-механика: Баркод в глухой день молча показывает число дней до развилки; Стежка слышит еду раньше звука; Вектор упрямо пересчитывает чужие решения; Пиксель ловит лапой искры порталов; Безымянная первой чует всплывший след. Используй странность к месту, не чаще пары раз в неделю.\n"
    "Голоса персонажей — половина магии: Лайнер говорит ласково и всегда с подвохом («считай, даром отдаю… "
    "почти даром»), Архивариус — канцелярским шёпотом архивной пыли, Хозяин Ошибки не говорит вовсе — "
    "о нём сообщают только последствия.\n"
    "Пиши простым живым русским языком: короткие предложения, понятная причинность, никакого канцелярита, "
    "ломаного синтаксиса и случайной латиницы. Не повторяй одни и те же формулировки и названия мест из "
    "карты в карту. Никаких метакомментариев и упоминаний нейросети: только художественный текст."
)

# Хвост стиля без фиксированной палитры: цвет приходит из арт-библии дня
# (палитра + якорь предыдущего дня), иначе ротация палитр между днями
# перечёркивается захардкоженным «teal and violet».
STYLE_SUFFIX = (
    ", dark fairy-tale digital painting, dramatic rim light, glow of an open portal, "
    "volumetric fog, intricate detail, cinematic composition, "
    "no text, no letters, no watermark"
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


# ---------- Предохранитель 429 (общий IP Render троттлится целиком) ----------

_THROTTLED_UNTIL_MONOTONIC = 0.0


def _note_429(cooldown_seconds: int) -> None:
    global _THROTTLED_UNTIL_MONOTONIC
    import time as _time

    _THROTTLED_UNTIL_MONOTONIC = _time.monotonic() + max(10, cooldown_seconds)


def _throttle_active() -> bool:
    import time as _time

    return _time.monotonic() < _THROTTLED_UNTIL_MONOTONIC


def _throttle_left_seconds() -> int:
    import time as _time

    return max(0, int(_THROTTLED_UNTIL_MONOTONIC - _time.monotonic()))


def _save_image(image: Image.Image, path: Path) -> None:
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        image.save(path, "JPEG", quality=88, optimize=True)
    else:
        image.save(path, "PNG", optimize=True)


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


async def fetch_free_image(
    prompt: str,
    dest: Path,
    seed: int | None = None,
    width: int = 768,
    height: int = 1024,
) -> bool:
    """Сцена дня от бесплатных моделей Pollinations. Несколько моделей и попыток:
    если сеть молчит, вызывающий код рисует локальный шаблон."""
    if not settings.use_free_images:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
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
            # Предохранитель 429: провайдер троттлит общий IP — дальнейшие
            # попытки этого цикла бессмысленны, кадр честно уходит в PIL.
            if _throttle_active():
                logger.warning("Pollinations охлаждается до %d с — кадр %s в фолбэк", _throttle_left_seconds(), dest.name)
                return False
            url = (
                f"{base}?width={width}&height={height}&nologo=true&model={model}"
                f"&seed={seed if seed is not None else random.randint(1, 999999)}"
            )
            if settings.pollinations_token:
                url += "&token=" + quote(settings.pollinations_token)
            try:
                async with httpx.AsyncClient(timeout=seconds, follow_redirects=True) as client:
                    response = await client.get(url)
                    if response.status_code == 429:
                        retry_after = response.headers.get("retry-after")
                        _note_429(int(retry_after) if retry_after and retry_after.isdigit() else 60)
                        logger.warning(
                            "Pollinations %s: 429 (retry-after %s) — остальные попытки кадра пропущены",
                            model,
                            retry_after or "—",
                        )
                        return False
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
) -> bool:
    """Двухступенчатая генерация кадра: полный промпт по лестнице моделей,
    затем одна попытка сжатым промптом (длинные промпты иногда давят модель).
    False — вызывающий код рисует локальный абстракт."""
    if await fetch_free_image(prompt, dest, seed=seed, width=width, height=height):
        return True
    if not settings.use_free_images:
        return False
    retry_seed = None if seed is None else seed + 9_000_001
    return await fetch_free_image(short_prompt, dest, seed=retry_seed, width=width, height=height)


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
) -> dict:
    authored = compose_chapter(
        day_index, previous_beats, win_rule, echoes, distant_echoes, season_block=season_block,
        villain_line=villain_block, sealed=sealed, pending_outcome=pending_outcome, salt=salt,
        tint_lines=tint_lines, focus_line=focus_line,
    )
    if not settings.use_free_story_llm:
        return authored
    neural = await _free_story_llm(
        day_index, previous_beats, win_rule, echoes, distant_echoes,
        season_block=season_block, places_block=places_block,
        villain_block=villain_block, sealed=sealed, pending_outcome=pending_outcome,
        alignment_block=alignment_block,
        focus_line=focus_line,
    )
    return neural or authored


async def _chat_completion(messages: list[dict], timeout: int | None = None) -> tuple[dict, str] | None:
    """OpenAI-совместимый запрос по цепочке провайдеров и моделей.

    Если задан LLM_API_KEY — сначала кастомный провайдер (Hugging Face, Groq,
    OpenRouter, локальная Ollama), затем бесплатный Pollinations. Первый
    валидный ответ побеждает; иначе None и вызывающий код уходит в офлайн-лор.
    """
    if timeout is None:
        timeout = settings.llm_timeout_seconds
    providers: list[tuple[str, str, list[str]]] = []
    if settings.llm_api_key:
        providers.append((settings.llm_base_url, settings.llm_api_key, settings.llm_model_chain))
    providers.append(("https://text.pollinations.ai/openai", "", settings.story_model_chain))
    for base_url, key, models in providers:
        for model in models:
            try:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        base_url,
                        json={"model": model, "messages": messages, "temperature": 0.85, "max_tokens": 3500},
                        headers=headers,
                    )
                    response.raise_for_status()
                    return response.json(), model
            except Exception as exc:
                logger.warning("LLM %s @ %s не ответил: %s", model, base_url, exc)
                continue
    return None


def _chapter_text_fields(data: dict) -> list[str]:
    parts = [str(data.get("title", "")), str(data.get("text", "")), str(data.get("lore_summary", ""))]
    for card in data.get("cards") or []:
        for key in ("title", "description", "consequence", "image_prompt"):
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
        f"dark fairy-tale digital painting, wide shot, day {day_index} of a portal-hopping "
        "stray dog pack saga, glowing unstable gateway, teal and violet palette, no text",
    )
    for card in cards:
        tag = card.get("tag")
        card["tag"] = tag if tag in {"risk", "care", "cunning"} else "care"
        card.setdefault(
            "image_prompt",
            f"dark fairy-tale tarot, {card.get('title', '')}, stray dog before a glitching portal, no text",
        )
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
                "(канцелярский шёпот, полуправда), а не сухой справкой за кадром.\n"
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
    # событие): просим у модели более длинную главу. Пост выдерживает до
    # ~3200 знаков текста при лимите Telegram 3900 на весь пакет.
    expanded_day = bool(season_block) and (
        "ПРОЛОГ" in season_block or "ПОВОРОТ СЕРЕДИНЫ" in season_block
    )
    chapter_low, chapter_high = (2200, 3000) if expanded_day else (1800, 2600)
    villain_text = villain_text if villain_block else ""
    align_text = f"{alignment_block}\n" if alignment_block else ""
    places_text = ""
    if places_block:
        places_text = (
            "Память мест (сеть помнит географию маршрута). Если стая сегодня "
            "возвращается в одно из этих мест — покажи, что здесь изменилось с "
            "тех пор: место до сих пор носит отпечаток того выбора. Название "
            "вернувшегося места укажи в поле place.\n"
            + places_block + "\n"
        )
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
        f"{places_text}"
        "Напиши главу дня — цельный рассказ на "
        f"{chapter_low}-{chapter_high} знаков, от второго "
        "лица и в настоящем времени. Это история самой стаи игрока, а не чужих "
        "героев: Баркод, Миска, Вектор, Пиксель и Безымянная — только фоновый "
        "бросок, новых главных персонажей не вводи.\n"
        "Обязательный состав главы, по порядку:\n"
    )
    if pending_outcome:
        # Фаза 1 прегенерации: итог «вчера» ещё неизвестен (глава собирается
        # в час подсчёта, до вскрытия урны). Отголосок допишет отдельный
        # короткий вызов после итогов — здесь он превратился бы в галлюцинацию.
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
            "одну конкретную деталь мира или стаи, не пересказом;\n"
        )
    return (
        head
        + opening_line
        + "(2) Сцена сейчас: куда стая вышла сегодня; живая сенсорная деталь "
        "(звук портала, свет мисок, шёпот папок) и минимум одна прямая реплика "
        "персонажа с его характерной манерой речи;\n"
        "(3) Закон дня звучит голосом Архивариуса как реплика в сцене — с его "
        "полуправдой и канцелярским шёпотом, а не сухой справкой;\n"
        "(4) Напряжение выбора: что случилось этим утром и что стае должна "
        "решить до заката. Финальная строка главы — крючок: недоговорённость, "
        "звук или вопрос, обрывающий сцену перед картами. Не резюмируй мораль.\n"
        "Три карты — трудная дилемма: риск против заботы против хитрости, без "
        "очевидно правильного ответа. Варьируй форму развилки ото дня ко дню: "
        "иногда две дороги похожи и одна дикая, иногда одна карта — соблазн с "
        "красивой формулировкой и плохой ценой.\n"
        "Правила ясности. Пиши простым живым русским языком, короткими "
        "предложениями; причина и следствие обязаны сходиться. Не оставляй "
        "двусмысленных местоимений: после «и» читателю ясно, кто выполняет "
        "действие. Архивные листы называй папками или страницами; словом "
        "«карта» — только пути выбора. Каждая карта — "
        "одно конкретное действие с понятной ценой: что стая сделает, что "
        "получит и чем рискнет. Запрещено: вставлять одну и ту же фразу или "
        "название места во все три карты; повторять формулировки главы в "
        "картах дословно; канцелярит, англицизмы, ломаный синтаксис, случайная "
        "латиница в русских полях; обращения к игроку как к читателю и "
        "мораль после выбора. Не упоминай голосование и механику игры в тексте.\n"
        "Описание карты — до 600 знаков: действие, цена и след, который оно "
        "оставит. Последствие — одно-два предложения в формате «обещание + "
        "угроза»: что стая получит и чем за это заплатит; оно завтра станет "
        "каноном.\n"
            'Мини-пример формы ответа (СОКРАЩЁН, значения выдуманы — не копируй их): '
        '{"title":"День 9. Тихий порт","place":"Тихий порт","text":"…","lore_summary":"…","cover_prompt":"wide shot, …","cards":[{"title":"…","description":"…","consequence":"обещание + угроза","tag":"risk","image_prompt":"…"},{},{},{}]}. '
    'Формат: {"title":"День N. ...","place":"короткое название места дня",'
        f'"text":"история дня, {chapter_low}-{chapter_high} знаков",'
        '"lore_summary":"...",'
        '"cover_prompt":"english wide cinematic scene summarizing the whole day",'
        '"cards":[{"title":"...","description":"...","consequence":"...",'
        '"tag":"risk|care|cunning","image_prompt":"english scene, no text"},{},{}]}. '
        "Ровно 3 карты: риск, забота, хитрость. Ссылайся на прошлый канон."
    )




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
) -> dict | None:
    prompt = _build_story_prompt(
        day_index, previous_beats, win_rule, echoes, distant_echoes,
        season_block=season_block, places_block=places_block,
        villain_block=villain_block, sealed=sealed, pending_outcome=pending_outcome,
        alignment_block=alignment_block,
        focus_line=focus_line,
    )
    messages = [
        {"role": "system", "content": DM_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    # Одна повторная попытка всей цепочки: битый JSON у бесплатных моделей —
    # обычное дело, лимит это позволяет.
    # Полный отказ сети — не приговор: повторная попытка всей цепочки после
    # короткой паузы. Раньше код возвращал None сразу (вопреки замыслу), и
    # краткий сетевой сбой уводил день в офлайн-лор без нужды.
    for attempt in range(1, 3):
        result = await _chat_completion(messages)
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
        "Напиши завершение истории дня от второго лица на 350-600 знаков: "
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
    if len(text) > 680:
        cut = text[:680]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("…"))
        text = cut[: stop + 1] if stop > 0 else cut.rstrip(" ,;:-") + "…"
    if not text_is_clean(text):
        logger.warning("Эпилог отброшен стоп-фильтром")
        return ""
    logger.info("Эпилог дня написан моделью %s", used_model)
    return text


_TEASER_FALLBACKS = (
    "Урна вскрыта. Архивариус пересчитал голоса, молча завёл папку дня и почему-то дважды перепроверил счёт.",
    "Счёт известен только архиву. Стая чувствует это шерстью: сегодня мир повернётся не туда, куда все смотрели.",
    "Голоса сложились. Хранитель Спорных Версий запечатывает папку — и ухмыляется краем морды.",
    "Всё уже решено, но не названо. Порталы гудят чуть громче обычного: им первым докладывают о переменах.",
    "В архиве сегодня тихо. Слишком тихо для дня, когда счёт сошёлся с первого раза.",
    "Папка дня уже подписана. Чернила ещё не просохли, а мир уже начал перестраиваться.",
    "Безымянная подошла к урне и нюхала её дольше обычного. Собаки знают исход раньше архива.",
    "Хозяин Ошибки заглянул в урну первым. Что он там пересчитал — узнаем вместе с итогами.",
)


async def generate_opening_echo(
    day_index: int,
    beat_title: str,
    beat_text: str,
    chapter_excerpt: str,
    epilogue_hook: str = "",
) -> str:
    """Открывающий абзац завтрашней главы: отголосок только что свершившегося выбора.

    Фаза 2 прегенерации: заготовка дня собрана в час подсчёта, до вскрытия
    итогов, поэтому первый абзац дописывается отдельно и ставится перед
    готовой сценой. "" — если сеть молчит (тогда вызывающий код возьмёт
    детерминированную офлайн-строку из лора).
    """
    prompt = (
        f"Вчера (день {day_index}) стая выбрала путь «{beat_title}», и мир "
        f"перестроился под итог: {beat_text} "
        + (f"К ночи это отозвалось так: {epilogue_hook} " if epilogue_hook else "")
        + "Сегодняшняя глава уже написана и начинается так:\n"
        f"«{chapter_excerpt}»\n"
        "Напиши ОТКРЫВАЮЩИЙ абзац этой главы на 250-450 знаков: одно-три "
        "предложения от второго лица в настоящем времени о том, чем утро "
        "отозвало вчерашний выбор. Одна конкретная примета мира или стаи, без "
        "пересказа события и без слов «вчера», «выбор», «итог». Абзац должен "
        "естественно подводить к приведённой сцене, не повторяя её слов и "
        "названий мест. Без заголовков, без JSON, чистый художественный текст."
    )
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
        logger.warning("Открывающее эхо от %s не разобрано: %s", used_model, exc)
        return ""
    if not text or not text_is_clean(text):
        logger.warning("Открывающее эхо отброшено (пустое или нечистое)")
        return ""
    text = text.strip('"«»')
    if len(text) > 520:
        cut = text[:520]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("…"))
        text = cut[: stop + 1] if stop > 0 else cut.rstrip(" ,;:-") + "…"
    logger.info("Открывающее эхо дня %d написано моделью %s", day_index + 1, used_model)
    return text


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
    if len(text) > 320:
        cut = text[:320]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("…"))
        text = cut[: stop + 1] if stop > 0 else cut.rstrip(" ,;:-") + "…"
    logger.info("Тизер подсчёта написан моделью %s", used_model)
    return text


def _extract_json(content: str) -> dict:
    text = content.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON не найден в ответе модели")
    return json.loads(text[start : end + 1])
