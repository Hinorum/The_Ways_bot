from __future__ import annotations

import json
import logging
import random
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
    "Ты — Ведущий (Dungeon Master) сюжетной игры в духе Dungeons & Dragons и "
    "Pathfinder, мастер тёмного фэнтези. Мир: Пепельный Тракт — сеть дорог между "
    "осколками городов, где память хранят не книги, а Следы решений толпы; "
    "безымянный пёс-проводник помнит все развилки, колокол звонит только когда "
    "стая ошибается. Веди игрока как настоящий Ведущий: второе лицо («ты»), "
    "настоящее время, плотная чувственная сцена — запах гари и мокрого пепла, "
    "звон колокола, шерсть пса под ладонью. Каждая развилка — трудная дилемма "
    "без очевидно правильного ответа, и каждое решение обязательно отзовётся "
    "позже, даже если не сразу. Никаких метакомментариев и механик вне "
    "заданного формата: только художественный текст на русском."
)

STYLE_SUFFIX = (
    ", grimdark oil painting, dramatic chiaroscuro lighting, muted ashen palette, "
    "volumetric fog, drifting embers, intricate detail, cinematic composition, "
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


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> str:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join(current + [word])
        if len(trial) <= width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    if len(lines) > 10:
        lines = lines[:10]
        lines[-1] = lines[-1].rstrip(" ,.;:") + "…"
    return "\n".join(lines)


def _save_image(image: Image.Image, path: Path) -> None:
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        image.save(path, "JPEG", quality=88, optimize=True)
    else:
        image.save(path, "PNG", optimize=True)


def render_card(path: Path, title: str, description: str, position: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    palettes = [
        ((22, 18, 16), (214, 118, 64), (255, 228, 190)),
        ((14, 24, 28), (84, 168, 168), (226, 244, 240)),
        ((26, 16, 36), (168, 96, 210), (242, 220, 255)),
    ]
    bg, accent, ink = palettes[position % 3]
    image = Image.new("RGB", (768, 1024), bg)
    overlay = Image.new("RGB", (768, 1024), accent)
    faded = Image.blend(image, overlay, 0.12).filter(ImageFilter.GaussianBlur(0.4))
    draw = ImageDraw.Draw(faded)
    draw.rounded_rectangle((28, 28, 740, 996), radius=40, outline=accent, width=6)
    draw.rounded_rectangle((64, 120, 704, 430), radius=28, fill=accent)
    draw.text((72, 52), f"ПУТЬ {['I', 'II', 'III'][position]}", fill=accent, font=_font(24))
    draw.multiline_text((84, 170), _wrap(title, 16), fill=bg, font=_font(44), spacing=6)
    draw.multiline_text((72, 480), _wrap(description, 26), fill=ink, font=_font(30), spacing=8)
    draw.text((72, 930), "ПЕПЕЛЬНЫЙ ТРАКТ", fill=accent, font=_font(22))
    _save_image(faded, path)


def render_cover(path: Path, title: str, body: str = "") -> None:
    """Локальная обложка дня: заголовок главы и завязка сюжета."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bg, accent, ink = (18, 15, 14), (214, 118, 64), (240, 222, 198)
    width, height = 1280, 720
    image = Image.new("RGB", (width, height), bg)
    overlay = Image.new("RGB", (width, height), accent)
    faded = Image.blend(image, overlay, 0.10).filter(ImageFilter.GaussianBlur(0.6))
    draw = ImageDraw.Draw(faded)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=36, outline=accent, width=6)
    draw.text((64, 56), "ПЕПЕЛЬНЫЙ ТРАКТ", fill=accent, font=_font(30))
    draw.multiline_text((64, 128), _wrap(title, 28), fill=ink, font=_font(58), spacing=10)
    if body:
        draw.multiline_text(
            (64, 330),
            _wrap(body.replace("\n", " ")[:520], 60),
            fill=(206, 190, 168),
            font=_font(26),
            spacing=6,
        )
    draw.text((64, height - 78), "СЮЖЕТ ДНЯ", fill=accent, font=_font(24))
    _save_image(faded, path)


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
    plans = [
        ("flux", 60, 1),
        ("sana", 45, 2),
        ("turbo", 30, 2),
    ]
    for model, seconds, attempts in plans:
        for attempt in range(1, attempts + 1):
            url = (
                f"{base}?width={width}&height={height}&nologo=true&model={model}"
                f"&enhance=true&seed={seed if seed is not None else random.randint(1, 999999)}"
            )
            try:
                async with httpx.AsyncClient(timeout=seconds, follow_redirects=True) as client:
                    response = await client.get(url)
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


async def generate_chapter(day_index: int, previous_beats: list[str], win_rule=None, echoes=None) -> dict:
    authored = compose_chapter(day_index, previous_beats, win_rule, echoes)
    if not settings.use_free_story_llm:
        return authored
    neural = await _free_story_llm(day_index, previous_beats, win_rule, echoes)
    return neural or authored


async def _chat_completion(messages: list[dict], timeout: int = 45) -> tuple[dict, str] | None:
    """OpenAI-совместимый запрос по цепочке провайдеров и моделей.

    Если задан LLM_API_KEY — сначала кастомный провайдер (Hugging Face, Groq,
    OpenRouter, локальная Ollama), затем бесплатный Pollinations. Первый
    валидный ответ побеждает; иначе None и вызывающий код уходит в офлайн-лор.
    """
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
                        json={"model": model, "messages": messages, "temperature": 0.85, "max_tokens": 2400},
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
    data.setdefault(
        "cover_prompt",
        f"dark fantasy matte painting, wide shot, day {day_index} of an ashen road saga, "
        "lone nameless dog guide, no text",
    )
    for card in cards:
        tag = card.get("tag")
        card["tag"] = tag if tag in {"risk", "care", "cunning"} else "care"
        card.setdefault(
            "image_prompt",
            f"dark fantasy tarot, {card.get('title', '')}, silent dog, no text",
        )
    return data


def _build_story_prompt(day_index: int, previous_beats: list[str], win_rule=None, echoes=None) -> str:
    """Промпт главы дня. Чистая функция — покрывается тестами без сети."""
    history = "\n".join(previous_beats[-8:]) or "история ещё не началась"
    law_line = ""
    if win_rule is not None:
        from app.models import RULE_PHRASES

        law_line = (
            f"Закон сегодняшнего дня уже объявлен игрокам с утра: {RULE_PHRASES[win_rule]}. "
            "Текст должен упоминать этот закон как известный факт, а не тайну.\n"
        )
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
    return (
        "Ответь только JSON. Русский язык. Ежедневная сюжетная игра в духе D&D. "
        f"День {day_index}. Канон прошлых дней:\n{history}\n"
        f"{law_line}"
        f"{echo_block}"
        "Напиши главу дня — цельный мини-рассказ на 400-900 знаков, от второго "
        "лица и в настоящем времени. Это история стаи самого игрока и "
        "пса-проводника, а не чужих героев: попутчики и встречные — только фон, "
        "новых главных персонажей не вводи. Строй рассказ тремя движениями: "
        "(1) предыстория — чем отозвался вчерашний выбор из канона, что он "
        "изменил в мире и в стае; (2) сцена сейчас — куда вывел Тракт и что "
        "происходит, с чувственными деталями: звон колокола, пепел на зубах, "
        "шерсть пса под ладонью; (3) напряжение выбора — что случилось этим "
        "утром и что стае нужно решить до заката. Три карты — трудная дилемма: "
        "риск против заботы против хитрости, без очевидно правильного ответа.\n"
        "Пиши грамотно: без орфографических ошибок, опечаток и ломаного "
        "синтаксиса. Проверяй окончания и пунктуацию. Избегай англицизмов "
        "и канцелярита, тон — тёмное фэнтези. Не вставляй латиницу в русские "
        "поля. Описание карты — до 140 знаков, последствие — одно короткое "
        "предложение, которое завтра станет каноном.\n"
        'Формат: {"title":"День N. ...","text":"история дня, 400-900 знаков",'
        '"lore_summary":"...",'
        '"cover_prompt":"english wide cinematic scene summarizing the whole day",'
        '"cards":[{"title":"...","description":"...","consequence":"...",'
        '"tag":"risk|care|cunning","image_prompt":"english scene, no text"},{},{}]}. '
        "Ровно 3 карты: риск, забота, хитрость. Ссылайся на прошлый канон."
    )


async def _free_story_llm(day_index: int, previous_beats: list[str], win_rule=None, echoes=None) -> dict | None:
    prompt = _build_story_prompt(day_index, previous_beats, win_rule, echoes)
    messages = [
        {"role": "system", "content": DM_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    # Одна повторная попытка всей цепочки: битый JSON у бесплатных моделей —
    # обычное дело, лимит это позволяет.
    for attempt in range(1, 3):
        result = await _chat_completion(messages)
        if result is None:
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
) -> str:
    """Финальный штрих итогов: чем отозвался победивший путь. "" — сеть молчит."""
    prompt = (
        f"День {day_index} закрылся. Закон дня был такой: {rule_phrase}. "
        f"Победил выбор «{winner_title}»: {winner_consequence} "
        f"Поддержка по трём дорогам была такая: {counts_line}. "
        "Напиши финальный штрих этого дня — одно или два предложения, до 220 знаков: "
        "что этот выбор только что сделал с миром, стаей и псом-проводником. "
        "Обращайся к игроку на «ты». Без цифр; без слов «голос», «итог», "
        "«канон», «путь», «выбор». Только художественный текст на русском."
    )
    result = await _chat_completion(
        [
            {"role": "system", "content": DM_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        timeout=30,
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
    if len(text) > 320:
        cut = text[:320]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("…"))
        text = cut[: stop + 1] if stop > 0 else cut.rstrip(" ,;:-") + "…"
    if not text_is_clean(text):
        logger.warning("Эпилог отброшен стоп-фильтром")
        return ""
    logger.info("Эпилог дня написан моделью %s", used_model)
    return text


def _extract_json(content: str) -> dict:
    text = content.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON не найден в ответе модели")
    return json.loads(text[start : end + 1])
