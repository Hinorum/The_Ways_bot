from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageOps

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
    "Лайнер-торговец — обменивает чужие воспоминания на проводу и помнит каждый долг стаи; он сам давно "
    "продал всю свою память и потому не помнит ничего — кроме одного: на бедре у него висит старое пыльное "
    "радио, которое молчит с начала времён и оживает только в ночи кризиса ровным ЛАЕМ-сигналом; "
    "Лайнер называет его «молчанием Сигнала» и торгует им, как любой другой памятью; "
    "Архивариус Хранитель Спорных Версий — объявляет законы дня как настроение архива и никогда не врёт, "
    "но говорит полуправдами; его очки-лупы в левом и правом стекле отражают РАЗНЫЙ текст одной страницы; "
    "Еретик, Свернувший с Пути — хозяин этой игры: пёс из старой Стаи, где один "
    "сон был на всех, заскучал и увёл стаю сюда переписывать правила; он не оправдывается и говорит "
    "короткими формулами («закон Волка — мой», «глухой день — тоже мой»), а его знак — апостроф; "
    "под пальто из старых карт он прячет выцветший ошейник старой Стаи и никогда о нём не говорит вслух; "
    "он всегда говорит о себе в третьем лице («тот, кто свернул…»), даже когда это очевидно про него; "
    "Хозяин Ошибки — антагонист без лица из того же старого мира: он пересчитывает стаю и чинит мир "
    "не так, мечтая вернуть всем ровный предсказуемый сон — скука для него победа; он не зол — он "
    "тихо скучает по тому ровному сну и «спасает» стаю от свободы, которая его пугает, поэтому его "
    "идеальная аккуратность всегда чуть грустная. У каждого своё "
    "отношение к стае, и оно меняется от её выборов: доброта запоминается, жестокость тоже. Пять собак "
    "стаи — Баркод, Стежка, Вектор, Пиксель и Безымянная — остаются фоном: именная деталь раз в несколько "
    "дней ), но не главные герои. У каждой — странность-механика: Баркод в глухой день молча показывает число дней до развилки; Стежка слышит еду раньше звука; Вектор упрямо пересчитывает чужие решения; Пиксель ловит лапой искры порталов; Безымянная первой чует всплывший след. Тайна сезона: старой игры Безымянная не существовало — Хозяин Ошибки ни разу не смог её пересчитать, потому что она и есть его первая «сломанная ошибка», научившаяся быть выбором; не раскрывай это прямо — позволь ей чуять Лай раньше других и вести стаю к финалу одним жестом. Используй странность к месту, не чаще пары раз в неделю.\n"
    "Голоса персонажей — половина магии: Лайнер говорит ласково и всегда с подвохом («считай, даром отдаю… "
    "почти даром»), Архивариус — канцелярским шёпотом архивной пыли и НИКОГДА не ставит точку в конце фразы, "
    "только многоточие («дело, знаешь ли, вот в чём…»); Еретик — сухо и по-уставу своего "
    "нового мира, короткими правилами вместо объяснений, Хозяин Ошибки не говорит вовсе — "
    "о нём сообщают только последствия.\n"
    "Ритуал стаи: когда мир напрягает уши, собаки говорят «Уши востро. Слушаем.» — это знак, что все "
    "вслушиваются в Первый Лай, а Лай для этого мира и есть Сигнал, который ждут из-под сети. Давай этот "
    "жест раз или два, когда стая затихает перед выбором.\n"
    "Пиши простым живым русским языком: короткие предложения, понятная причинность, никакого канцелярита, "
    "ломаного синтаксиса и случайной латиницы. Не повторяй одни и те же формулировки и названия мест из "
    "карты в карту. Никаких метакомментариев и упоминаний нейросети: только художественный текст."
)

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
            # Предохранитель 429: именно эта модель охлаждается. Не обрываем
            # весь кадр — соседняя модель (sana/turbo) может ещё отдать картинку.
            if _throttle_active(model):
                logger.warning(
                    "Pollinations %s охлаждается до %d с — пробуем другую модель",
                    model, _throttle_left_seconds(model),
                )
                break
            url = (
                f"{base}?width={width}&height={height}&nologo=true&private=true&model={model}"
                f"&seed={seed if seed is not None else random.randint(1, 999999)}"
            )
            if settings.pollinations_token:
                url += "&token=" + quote(settings.pollinations_token)
            try:
                async with httpx.AsyncClient(timeout=seconds, follow_redirects=True) as client:
                    response = await client.get(url)
                    if response.status_code == 429:
                        retry_after = response.headers.get("retry-after")
                        _note_429(model, int(retry_after) if retry_after and retry_after.isdigit() else 60)
                        logger.warning(
                            "Pollinations %s: 429 (retry-after %s) — охлаждение модели, пробуем другую",
                            model,
                            retry_after or "—",
                        )
                        break
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
    """Лестница кадра: Gemini «nano banana» → Pollinations (полный промпт,
    потом сжатый — длинные промпты иногда давят модель). False — вызывающий
    код рисует локальный абстракт. Один сетевой кадр в день делает лестницу
    практически безошибочной: ни один провайдер не успевает затроттлиться."""
    if await _fetch_gemini_image(prompt, dest, width=width, height=height):
        return True
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
    )
    # Типографика применяется к обоим путям: нейро-текст приходит с
    # ASCII-кавычками и дефисами, офлайн-сборка проходит для гарантии.
    return _polish_chapter(neural or authored)


class _LLMRateLimited(Exception):
    """Внутренний сигнал: провайдер сбросил на 429 — пробуем следующую модель."""


async def _chat_completion(messages: list[dict], timeout: int | None = None) -> tuple[dict, str] | None:
    """OpenAI-совместимый запрос по цепочке провайдеров и моделей.

    Если задан LLM_API_KEY — сначала кастомный провайдер (Hugging Face, Groq,
    OpenRouter, локальная Ollama), затем бесплатный Pollinations. Первый
    валидный ответ побеждает; иначе None и вызывающий код уходит в офлайн-лор.

    Устойчивость здесь общая для ВСЕХ текстовых генераторов (эпилог, открывающее
    эхо, тизер, шёпот, арт-библия — у части из них отдельных повторов нет вовсе):
      - 429: читаем retry-after (с потолком) и переходим к следующей модели,
        не обрушая весь вызов;
      - полный сбой цепочки: один повтор всего провайдера после короткой паузы,
        чтобы краткий сетевой blip не обнулял генерацию.
    """
    if timeout is None:
        timeout = settings.llm_timeout_seconds
    providers: list[tuple[str, str, list[str]]] = []
    if settings.llm_api_key:
        providers.append((settings.llm_base_url, settings.llm_api_key, settings.llm_model_chain))
    providers.append(("https://text.pollinations.ai/openai", "", settings.story_model_chain))
    for overall_attempt in range(1, 3):
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
                        if response.status_code == 429:
                            retry_after = response.headers.get("retry-after", "")
                            pause = min(int(retry_after), 10) if retry_after.isdigit() else 4
                            logger.warning("LLM %s @ %s: 429 — пауза %d с, следующая модель", model, base_url, pause)
                            await asyncio.sleep(pause)
                            raise _LLMRateLimited()
                        response.raise_for_status()
                        return response.json(), model
                except _LLMRateLimited:
                    continue
                except Exception as exc:
                    logger.warning("LLM %s @ %s не ответил: %s", model, base_url, exc)
                    continue
        if overall_attempt == 1:
            logger.warning("Все модели LLM недоступны — повтор цепочки через 3 с")
            await asyncio.sleep(3)
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
        f"flat 2D vector cartoon, cozy-dystopia, bold clean outlines, muted matte colors, "
        f"wide shot, day {day_index} of a portal-hopping stray dog pack saga, "
        "glowing unstable gateway, dusty teal and burnt orange palette, no text",
    )
    for card in cards:
        tag = card.get("tag")
        card["tag"] = tag if tag in {"risk", "care", "cunning"} else "care"
        card.setdefault(
            "image_prompt",
            f"flat 2D vector cartoon tarot card, cozy-dystopia, bold outlines, {card.get('title', '')}, "
            "stray dog before a glitching portal, no text",
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
        "героев: Баркод, Стежка, Вектор, Пиксель и Безымянная — только фоновый "
        "бросок, новых главных персонажей не вводи. В дни пролога фокус сцены — "
        "одно вводимое лицо; остальные постоянные лица молчат фоном без реплик.\n"
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
            "одну КОНКРЕТНУЮ деталь мира или стаи: предмет, шрам на местности, "
            "запах, постройку. ЗАПРЕЩЕНЫ мета-фразы вроде «напоминает о "
            "вчерашнем выборе» и любые отсылки к факту голосования — только "
            "то, что изменилось в мире;\n"
        )
    return (
        head
        + opening_line
        + "(2) Сцена сейчас: куда стая вышла сегодня; живая сенсорная деталь "
        "(звук портала, свет мисок, шёпот папок) и минимум одна прямая реплика "
        "персонажа с его характерной манерой речи;\n"
        "(3) Закон дня звучит голосом Архивариуса как реплика в сцене — с его "
        "полуправдой и канцелярским шёпотом, а не сухой справкой;\n"
        "(4) Напряжение выбора: что случилось этим утром и какое решение стая "
        "должна успеть принять до темноты сети. Финальная строка главы — "
        "крючок: недоговорённость, звук или вопрос, обрывающий сцену перед "
        "картами. Не резюмируй мораль.\n"
        "Три карты — трудная дилемма: риск против заботы против хитрости, без "
        "очевидно правильного ответа. Варьируй форму развилки ото дня ко дню: "
        "иногда две дороги похожи и одна дикая, иногда одна карта — соблазн с "
        "красивой формулировкой и плохой ценой. Начала трёх карт различны: "
        "первое слово и конструкция каждой — свои.\n"
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
                # Контроль длины (инцидент: модель отдала конспект на ~1000
                # знаков, и он ушёл в пост). Нейро-глава обязана набрать
                # контрактный минимум; ниже — отклонение, вызывающий код
                # соберёт офлайн-версию (её аварийный пол ниже: 1000).
                expanded = bool(season_block) and (
                    "ПРОЛОГ" in season_block or "ПОВОРОТ СЕРЕДИНЫ" in season_block
                )
                min_chars = 1500 if expanded else 1200
                text_len = len(str(data.get("text", "")))
                if text_len < min_chars:
                    logger.warning(
                        "Модель %s вернула главу %d знаков (<%d, попытка %d) — отклонена",
                        used_model, text_len, min_chars, attempt,
                    )
                    continue
                # Верхний потолок: болезненно длинная глава режется по границе
                # предложения, а не заводит день с простыней и риском обрыва в ТГ.
                data["text"] = _clamp_sentence(str(data.get("text", "")), 4600)
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
    text = _clamp_sentence(text, 680)
    # Нижний порог: эпилог просят 350-600 знаков; конспект короче 140 — мусор,
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
    text = _clamp_sentence(text, 520)
    # Нижний порог: открывающий абзац просят 250-450 знаков — короче 120 это
    # бессвязный огрызок; пусть возьмётся детерминированная офлайн-строка.
    if len(text) < 120:
        logger.warning("Открывающее эхо %d знаков (<120) отброшено", len(text))
        return ""
    logger.info("Открывающее эхо дня %d написано моделью %s", day_index + 1, used_model)
    return polish_typography(text)


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
