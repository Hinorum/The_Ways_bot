from __future__ import annotations

import asyncio
import logging
from random import Random as _Random

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.broadcast import announce_new_day, send_personal_echoes
from app.config import settings
from app.db import SessionLocal
from app.models import Round, RoundStatus, WatcherState
from app.rounds import (
    _now,
    claim_announcement,
    close_voting,
    ensure_current_round,
    finish_tally,
    get_latest_round,
    utc_aware,
)
from app.story import _TEASER_FALLBACKS
from app.tally import award_points


logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=settings.timezone)
_bot: Bot | None = None


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


# Кадр вечернего костра. Темы-константы держим здесь, чтобы вечерний «привал»
# оценивался как самостоятельная сцена: стая у огня, невыбранные карты,
# воздух висящей развилки. 1 кадр в день — отдельная генерация от обложки.
_EVENING_CAMP_SCENE = (
    "a small circle of warm campfire light in the dark around the stray dog "
    "pack at night, the dogs lying close to the fire facing several weathered "
    "cards laid on the ground, one card reflected in their eyes, heavy unspoken "
    "tension in the air of a choice not yet made, faint moving shadows at the "
    "edge of the firelight, pinprick stars above"
)
_EVENING_CAMP_ART = "day{day_index}_camp.jpg"


def _campfire_bible(round_row, day_bible: dict | None = None) -> dict:
    """Минимальная библия вечернего кадра: костёр, стая, невыбранные карты.

    day_bible — полная библия дня из watcher_state: вечер наследует палитру,
    свет и мотивы утреннего кадра, чтобы день выглядел одним миром (утро-ночь),
    а костёр не прыгал в чужие цвета.
    """
    place = (getattr(round_row, "place", None) or "").strip()
    scene = _EVENING_CAMP_SCENE
    if place:
        scene = f"{scene}, the {place} stretching dark beyond the fire"
    if day_bible:
        motifs = [str(m) for m in (day_bible.get("motifs") or [])][:3]
        motifs.append("a card glinting faintly in the firelight")
        return {
            "shots": {"cover": {"scene": scene, "composition": ""}},
            "palette": str(day_bible.get("palette") or "deep indigo and ember orange"),
            "lighting": str(day_bible.get("lighting") or "low firelight, long soft shadows"),
            "motifs": motifs,
        }
    return {
        "shots": {"cover": {"scene": scene, "composition": ""}},
        "palette": "deep indigo and ember orange",
        "lighting": "low firelight, long soft shadows",
        "motifs": ["a card glinting faintly in the firelight"],
    }


async def _campfire_art(day_index: int, round_row) -> str | None:
    """Кадр вечернего костра: 1 генерация/день. None — кадр не получился."""
    from pathlib import Path

    from app.art_director import build_image_prompt, short_image_prompt
    from app.rounds import _load_day_bible
    from app.story import fetch_day_image, render_cover

    async with SessionLocal() as session:
        day_bible = await _load_day_bible(session, day_index)
    dest = Path(settings.media_dir) / _EVENING_CAMP_ART.format(day_index=day_index)
    seed = 40_000 + day_index * 11
    bible = _campfire_bible(round_row, day_bible)
    fetched = await fetch_day_image(
        build_image_prompt(bible, "cover", seed=seed),
        short_image_prompt(bible, "cover", seed=seed),
        dest,
        seed=seed,
        width=1280,
        height=720,
    )
    if not fetched:
        await asyncio.to_thread(
            render_cover,
            dest,
            f"Вечерний привал · день {day_index}",
            round_row.chapter_title or "",
        )
    return str(dest) if dest.exists() else None


async def _day_candidates(session, round_id: int) -> list[tuple[str, str]]:
    """Публичные карты дня: (название, последствие) по позициям.

    Возвращаются только публичные данные — названия и последствия видны всем
    игрокам. Никаких цифр голосов и победителя: их вечер не называет.
    """
    from sqlalchemy import select

    from app.models import Card

    rows = (
        await session.execute(
            select(Card)
            .where(Card.round_id == round_id)
            .order_by(Card.position)
        )
    ).scalars()
    return [(c.title, c.consequence or "") for c in rows if c.title]


async def _micro_event_job(round_id: int, day_index: int) -> None:
    """Вечерний привал: микросцена-продолжение утренней главы в прайм-тайм,
    чтобы вечер жил между утром и итогами. Падения полностью некритичны."""
    try:
        from app.db import SessionLocal
        from app.models import Round, WatcherState

        marker = f"micro_event:{round_id}"
        async with SessionLocal() as session:
            row = await session.get(WatcherState, marker)
            if row is not None:
                return
            round_row = await session.get(Round, round_id)
            if round_row is None or round_row.status != RoundStatus.OPEN:
                return
            from app.rounds import get_run_anchor
            from app.season import run_position
            from app.story_arc import arc_stage_index

            anchor = await get_run_anchor(session)
            moment = utc_aware(round_row.voting_ends_at)
            run_day, total = run_position(anchor, moment)
            arc_stage = arc_stage_index(run_day, total)
            season_hint = (
                "ДЕНЬ ПЕРВОГО ЛАЯ"
                if run_day >= total
                else f"до Дня Первого Лая {total - run_day} дн."
            )
            chapter_excerpt = (round_row.chapter_text or "")[:700]
            intrigue = day_index % 3 == 0
            # ARG: каждую седьмую неделю забега вместо микросцены стая
            # находит страницу Совета Хранителей (планы бота как канон).
            from app.council import page_for_run_day

            council_page = page_for_run_day(run_day)
            candidates = _day_candidates(session, round_id)
            text = (
                council_page
                if council_page is not None
                else await _compose_whisper(
                    day_index, season_hint, chapter_excerpt,
                    intrigue=intrigue,
                    candidates=candidates,
                    arc_stage=arc_stage,
                )
            )
            session.add(WatcherState(key=marker, value="1"))
            await session.commit()
        from aiogram.types import FSInputFile

        from app.broadcast import whisper_photo_to_chats, whisper_to_chats

        camp_path = None
        try:
            camp_path = await _campfire_art(day_index, round_row)
        except Exception:
            # Кадр не должен губить вечерний текст: падаем на обычный шёпот.
            logger.warning("Кадр костра дня %s не получился (исключение)", day_index, exc_info=True)
            camp_path = None
        if camp_path and text:
            await whisper_photo_to_chats(_bot, FSInputFile(camp_path), text)
        else:
            await whisper_to_chats(_bot, text)
    except Exception:
        logger.exception("Вечерняя микросцена не удалась (не мешает тику)")


async def _compose_whisper(
    day_index: int,
    season_hint: str,
    chapter_excerpt: str = "",
    intrigue: bool = False,
    candidates: list[tuple[str, str]] | None = None,
    arc_stage: int | None = None,
) -> str:
    """Микросцена вечера: нейротекст с офлайн-фолбэком. Не раскрывает ни эхи,
    ни расклад голосов — только продолжает утреннюю сцену одной репликой. Если
    переданы кандидаты (публичные карты дня), текст сильнее «чувствует»
    висящую развилку и переплетённость путей — без имён победителя."""
    import random as _random

    from app.story import DM_SYSTEM_PROMPT, _chat_completion, text_is_clean

    hint = ""
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import LoreEcho

        async with SessionLocal() as session:
            echo = (
                await session.execute(
                    select(LoreEcho)
                    .where(
                        LoreEcho.status == "dormant",
                        LoreEcho.earliest_day <= day_index + 2,
                    )
                    .order_by(LoreEcho.strength.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if echo is not None:
            texture = {
                "угроза": "в воздухе иногда пахнет жжёной проводкой",
                "память": "чужой тёплый свет вспоминает миски",
                "обман": "среди теней мелькает чужой силуэт",
            }.get(echo.kind, "портал гудит не в такт")
            hint = f"Осторожная примета дня (без деталей и без слов «эхо»): {texture}."
    except Exception:
        hint = ""

    cards_line = ""
    if candidates:
        names = "», «".join(title for title, _ in candidates if title)
        if names:
            cards_line = (
                f"\nКарты вечера на столе — пути, которые стая ещё не выбрала: "
                f"«{names}». Решение не принято, но мир уже ощущает тяжесть "
                f"этой развилки: пути тянут в разные стороны, и стая чувствует "
                f"переплетённость выбора кожей.\n"
            )

    task = (
        "Вечерняя ИНТРИГА: поставь утреннюю примету под сомнение одной "
        "деталяю или вопросом, которого никто не произнёс вслух; "
        "финал — недоговорённость."
        if intrigue
        else (
            "Напиши микросцену вечера: 2-4 предложения (до 450 знаков). "
            "Стая у карт костра, сцена дотянулась до заката; одна прямая "
            "реплика персонажа в его манере речи; финал — недоговорённость "
            "перед закрытием развилки. Пусть сквозит трепет от того, как "
            "близко решение и как переплетены ещё не выбранные пути — "
            "шерсть встаёт дыбом от тяжести выбора."
        )
    )
    messages = [
        {"role": "system", "content": DM_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Вечерний привал, день {day_index}. Сезон: {season_hint}. "
                f"{hint} "
                + (
                    f"Утренняя глава дня начиналась так:\n«{chapter_excerpt}»\n"
                    if chapter_excerpt
                    else ""
                )
                + cards_line
                + task
                + "Без цифр голосов, без имён победителя и без намёков на "
                "текущий расклад. Ответь чистым текстом, без JSON."
            ),
        },
    ]
    result = await _chat_completion(messages, timeout=45)
    if result is not None:
        try:
            text = str(result[0]["choices"][0]["message"]["content"]).strip()
            if text and text_is_clean(text):
                return text[:600]
        except Exception:
            pass
    rng = _random.Random(f"whisper:{day_index}")
    return _offline_whisper(
        day_index, season_hint, chapter_excerpt, candidates, intrigue, rng, arc_stage=arc_stage
    )


_WHISPER_FALLBACKS = (
    "Вечер стянул туман к самой земле. Стая легла вокруг карт, и никто не притронулся к ним первым.",
    "Лайнер прошёл мимо, позвякивая склянками с чужими снами: «Решайте, решайте… я только послушать».",
    "Архивариус захлопнул папку дня ровно на середине. «До утра всё может стать иначе», — шепнул он.",
    "Миски остыли, не тронутые. Баркод смотрел то на одну карту, то на другую, будто ждал подсказки.",
    "Безымянная встала мордой к порталу и не двинулась до темноты. Собаки чувствуют развилки кожей.",
    "Порталы загудели ниже обычного — почти шёпотом. Так сеть прислушивается к ещё не принятым решениям.",
    "Вектор обошёл карты по кругу трижды и лёг спиной к развилке. Упрямство тоже способ ждать.",
    "Где-то за холмом щёлкнул счётчик архива и замолчал на середине цифры. Никто не спросил, какой.",
    "Ветер принёс запах страницы, которую ещё не написали. Пиксель заворчал на пустоту.",
    "Хозяин Ошибки сегодня не считал вслух. Тишина от него страшнее любого счёта.",
    "Из подвала доносилось шипение: «Этот выбор котировался на 47%… ошибка рынка…».",
    "У портала лежала обглоданная табличка — номер вне каталога, но кто-то упорно её грыз.",
    "«В прошлом круге здесь голосовало больше», — шипела тьма из-под лап. «Но не правильнее».",
    "Тени легли к картам, хотя света уже не было. Вечер здесь не спрашивает разрешения у физики.",
    "Стая переглянулась: до закрытия развилки оставалась ночь, а решение всё ещё было только одно — на всех.",
    "Костёр выхватывал из темноты то одну карту, то другую, и каждая на миг становилась настоящей. Стая молчала — но в этом молчании слышалось, как передвигаются ещё не выбранные пути.",
    "Лапа Вектора зависла над картами и легла на пустое место между ними. «Сначала мир», — сказал он, и собак передёрнуло от того, что это значило.",
    "Сполохи озаряли лица по очереди, и у каждого путь на миг проступал в глазах. Развилка почти что выбрала саму себя — но никто ещё не решился это признать.",
)

# Предвестия суда: голос Анубиса звучит только в последние дни круга (день 27+),
# когда цикл подходит к точке, где его можно разомкнуть.
_WHISPER_ANUBIS = (
    "Тени сегодня длиннее, чем должны быть. Стежка рычала на свою тень — она не узнавала запах.",
    "Песок сыпался из пустой миски. Пиксель ловил искры, но они падали медленнее, чем положено.",
    "Эхо отозвалось дважды. Никто не лаял — но ответ пришёл. Безымянная смотрела на весы, которых не было.",
    "Где-то за холмом бесшумно качнулась чаша. Ветер не дул.",
    "Анубис ещё не пришёл, но костёр уже горел в низком золоте — будто весы репетировали свой свет.",
)

_WHISPER_OPENERS = (
    "Костёр озаряет карты вечера — «{names}»: стая легла вокруг огня, "
    "и никто не назвал выбранный путь вслух.",
    "У огня лежат карты вечера — «{names}»: развилка ждёт утра, а мир уже "
    "перебирает последствия невыбранного.",
    "Вечер выложил перед стаей пути — «{names}»: пламя дрожит над картами, "
    "как нетерпеливая сеть, и в этом молчании есть что-то слишком внимательное.",
)

_WHISPER_NO_CARDS_OPENERS = (
    "Вечер стянул туман к земле и задумчиво обошёл лагерь: карт под лапами "
    "нет, но развилка всё равно висит в воздухе. Стая молча ждёт утра.",
    "Лагерь притих раньше обычного: вечер был бы простым, если бы мир не ждал "
    "решения там, где его вроде бы никто не спрашивал.",
)


def _sentence_lead(text: str, limit: int = 160) -> str:
    """Первое предложение отрывка главы (для офлайн-привязки вечера к утру)."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    cut = text[:limit]
    best = -1
    for sep in (".", "!", "?", "…"):
        best = max(best, cut.rfind(sep))
    return cut[: best + 1] if best > 20 else ""


def _offline_whisper(
    day_index: int,
    season_hint: str,
    chapter_excerpt: str = "",
    candidates: list[tuple[str, str]] | None = None,
    intrigue: bool = False,
    rng: _Random | None = None,
    arc_stage: int | None = None,
) -> str:
    """Офлайн-шёпот, привязанный к дню: называет публичные карты вечера и
    отзывается на утреннюю главу вместо случайной строки из общего пула.
    При переданном этапе арки сцена берётся из пула этого этапа месяца."""
    import random as _random

    rng = rng or _random.Random(f"whisper:{day_index}")
    names = "», «".join(title for title, _ in (candidates or []) if title)
    parts: list[str] = []
    if names:
        parts.append(rng.choice(_WHISPER_OPENERS).format(names=names))
    else:
        parts.append(rng.choice(_WHISPER_NO_CARDS_OPENERS))
    if arc_stage is not None:
        from app.story_arc import whisper_pool_for_stage

        parts.append(rng.choice(whisper_pool_for_stage(arc_stage)))
    elif day_index >= 27:
        # Последние дни круга: предвестия Анубиса перекрывают общий пул.
        parts.append(rng.choice(_WHISPER_ANUBIS))
    else:
        parts.append(rng.choice(_WHISPER_FALLBACKS))
    if intrigue:
        lead = _sentence_lead(chapter_excerpt)
        if lead:
            parts.append(
                f"Утром глава обещала: «{lead}» — к ночи это обещание "
                "обзавелось вторым дном, и никто не произнёс вслух, каким именно."
            )
    text = " ".join(parts)
    return text[:600]


async def _teaser_job(round_id: int) -> None:
    """Тизер в час подсчёта: выбор сделан, победитель не называется.

    Драматургическая пауза между «стая проголосовала» и «итоги»: одна-две
    строки недосказанности во все чаты. Падения полностью некритичны.
    """
    try:
        marker = f"teaser:{round_id}"
        async with SessionLocal() as session:
            row = await session.get(WatcherState, marker)
            if row is not None:
                return
            round_row = await session.get(Round, round_id)
            if round_row is None or round_row.status != RoundStatus.TALLYING:
                return
            from app.models import RULE_MASKS, RULE_PHRASES

            rule_phrase = RULE_PHRASES[round_row.win_rule]
            mask_title, mask_mood = RULE_MASKS[round_row.win_rule]
            sealed = bool(getattr(round_row, "sealed", False))
        from app.story import generate_teaser

        text = await generate_teaser(round_row.day_index, rule_phrase)
        if not text:
            import random as _random

            text = _random.Random(f"teaser:{round_id}").choice(list(_TEASER_FALLBACKS))
        text = f"Маска дня — «{mask_title}»: {mask_mood}. {text}"
        if sealed:
            text += " И это ещё не всё: закон дня вскроется вместе с итогами."
        async with SessionLocal() as session2:
            session2.add(WatcherState(key=marker, value="1"))
            await session2.commit()
        from app.broadcast import whisper_to_chats

        delivered = await whisper_to_chats(_bot, f"🕯 {text}")
        logger.info("Тизер подсчёта дня %s разослан в %d чат(ов)", round_id, delivered)
    except Exception:
        logger.exception("Тизер подсчёта не удался (не мешает тику)")


async def _image_upgrade_job(day_index: int) -> None:
    """Отложенная перерисовка PIL-заглушек: пик 429 спадает за четверть часа."""
    try:
        await asyncio.sleep(900)
        from app.rounds import upgrade_stub_images

        upgraded = await upgrade_stub_images(day_index)
        if upgraded:
            logger.info("Картинки дня %d обновлены: %d кадр(ов) перерисовано", day_index, upgraded)
    except Exception:
        logger.exception("Апгрейд картинок дня %s не удался (не мешает тику)", day_index)


async def _personal_echo_job(round_id: int) -> None:
    """Личное эхо проигравшим голосующим: чем пахла бы их тропа.

    At-most-once: маркер ставится до отправок, чтобы сбой рассылки не
    превращался в повторные сообщения тем же игрокам. Падения некритичны.
    """
    try:
        marker = f"pecho:{round_id}"
        async with SessionLocal() as session:
            already = await session.get(WatcherState, marker)
            if already is not None:
                return
            session.add(WatcherState(key=marker, value="1"))
            await session.commit()
        async with SessionLocal() as session:
            round_row = (
                await session.execute(
                    select(Round)
                    .options(selectinload(Round.cards))
                    .where(Round.id == round_id, Round.status == RoundStatus.CLOSED)
                    .limit(1)
                )
            ).scalar_one_or_none()
        delivered = await send_personal_echoes(_bot, round_row)
        logger.info("Личное эхо дня %s доставлено %d игроку(ам)", round_id, delivered)
    except Exception:
        logger.exception("Личные эха дня %s не разосланы (не мешает тику)", round_id)


async def tick(bot: Bot | None = None) -> None:
    bot = bot or _bot
    from app.ops import is_game_paused, mark_tick

    await mark_tick()
    # Стоп-кран: дни не открываются и не закрываются, анонсы молчат.
    # Watcher (отдельная джоба) продолжает возвращать входящие переводы,
    # а очередь выплат — разгребаться: чужие деньги зависнуть не должны.
    async with SessionLocal() as session:
        if await is_game_paused(session):
            return
    async with SessionLocal() as session:
        previous = await get_latest_round(session)
        current = await ensure_current_round(session)

        # Самолечение: дни, застрявшие не-закрытыми позади актуального
        # (сбой доставки анонса, гонка /advance), дочитываются сами —
        # подсчёт и канон завершаются, ставки уходят в очередь выплат.
        try:
            from app.rounds import heal_stale_rounds

            healed = await heal_stale_rounds(session)
            if healed:
                logger.warning("Вылечено застрявших дней: %d", healed)
        except Exception:
            logger.exception("Лечение застрявших дней упало (не мешает тику)")

        # Прогрев кэшей для синхронных постов: якорь забега и живой банк дня.
        from app.rounds import get_run_anchor, refresh_round_pot_cache

        try:
            await get_run_anchor(session)
        except Exception:
            logger.exception("Якорь забега не прочитан (кэш останется прежним)")
        if current.status == RoundStatus.OPEN and settings.ton_enabled and current.money_mode:
            try:
                await refresh_round_pot_cache(session, current)
            except Exception:
                logger.exception("Банк дня не обновлён (кэш останется прежним)")

        # Первый запуск или только что созданный день — анонсим без итогов.
        # claim_announcement гарантирует ровно один пост на день, даже если
        # после деплоя секунду работают два процесса.
        if previous is None or current.id > previous.id:
            if await claim_announcement(session, current):
                await announce_new_day(bot, current)

        now = _now()
        if current.status == RoundStatus.OPEN and now >= utc_aware(current.voting_ends_at):
            # БЕСШОВНОЕ ЗАКРЫТИЕ: подсчёт мгновенный — не выходим из тика,
            # а проваливаемся дальше к финализации в этом же проходе.
            await close_voting(session, current)
        # Вечерний привал: один раз за день, в настраиваемый час (прайм-тайм).
        if (
            current.status == RoundStatus.OPEN
            and now.hour == settings.whisper_hour_utc % 24
        ):
            from app.models import WatcherState

            marker = f"micro_event:{current.id}"
            async with SessionLocal() as session:
                already = await session.get(WatcherState, marker)
            if already is None:
                asyncio.create_task(_micro_event_job(current.id, current.day_index))
        if current.status == RoundStatus.TALLYING and now < utc_aware(current.tally_ends_at):
            # ЛЕГАСИ-окно (старые раунды с часом подсчёта): следующий день
            # откроется синхронной генерацией в финализации. Новая сетка
            # проходит здесь насквозь мгновенно.
            # Тизер ожидания: раз за день, сразу после закрытия голосования.
            from app.models import WatcherState

            marker = f"teaser:{current.id}"
            async with SessionLocal() as session:
                already = await session.get(WatcherState, marker)
            if already is None:
                asyncio.create_task(_teaser_job(current.id))
        if current.status == RoundStatus.TALLYING and now >= utc_aware(current.tally_ends_at):
            finished, closed_here = await finish_tally(session, current)
            if closed_here:
                await award_points(session, finished)
                # Финализуем всегда, а не только при включённом TON:
                # если флаг погасили посреди дня со ставками, долг игрокам
                # должен остаться видимым (очередь+алерты), а не исчезнуть.
                from app.stakes import finalize_day_payouts

                await finalize_day_payouts(session, finished)
                # Вознаграждения победителям: мгновенный пинок диспетчера,
                # деньги уходят в течение пары минут после итогов.
                asyncio.create_task(_payout_dispatch_job())
                # ИТОГИ СРАЗУ: быстрый пост (только БД), без нейро-эпилога и
                # без ожидания нового дня — пользователи видят результат немедленно.
                asyncio.create_task(_announce_results_job(finished.id))
                # Тяжёлая часть уходит в фон: эпилог (нейро) → обложка →
                # материализация нового дня → пост дня → личные эха.
                # Новый день откроется «чуть позже», когда будет готов контент.
                asyncio.create_task(_finalize_new_day_job(finished.id))


async def _announce_results_job(finished_id: int) -> None:
    """Мгновенная рассылка сухих итогов дня (без эпилога и нового дня).

    Дёргается отдельной джобой сразу после вскрытия, чтобы не ждать
    нейро-контент нового дня. Своя сессия — запущена из тика после закрытия
    его собственной транзакции.
    """
    try:
        from app.broadcast import announce_results
        from app.models import Round

        async with SessionLocal() as session:
            finished = await session.get(Round, finished_id)
            if finished is None:
                logger.warning("Итоги дня %s: раунд не найден", finished_id)
                return
            await announce_results(_bot, finished)
    except Exception:
        logger.exception("Итоги дня %s не разосланы (не мешает тику)", finished_id)


async def _finalize_new_day_job(finished_id: int) -> None:
    """Тяжёлая доработка нового дня — фоном, по готовности нейро-контента.

    Итоги уже разосланы отдельно (_announce_results_job). Здесь: эпилог →
    пост эпилога → новый день (инлайн-генерация по итогу «вчера») → пост дня →
    личные эха. Итоги не дублируются: announce_new_day зовётся без finished.
    Свои краткоживущие сессии (нельзя переиспользовать сессию тика — она за
    пределами этого контекста).
    """
    from app.models import Round

    try:
        from app.broadcast import announce_epilogue, announce_new_day
        from app.rounds import create_next_round_detailed, write_epilogue

        # 1. Эпилог подтверждает выбор и закрепляется в БД (идемпотентно).
        # cards грузим сразу: write_epilogue ходит по ним синхронно, ленивая
        # подгрузка вне await дала бы MissingGreenlet.
        async with SessionLocal() as session:
            finished = (
                await session.execute(
                    select(Round).where(Round.id == finished_id).options(selectinload(Round.cards))
                )
            ).scalar_one_or_none()
            if finished is None:
                logger.warning("Доработка дня %s: раунд не найден", finished_id)
                return
            # Индекс дня берём из живой сессии: ниже finished расцепляется —
            # читать его day_index из отвязанного объекта было бы ошибкой.
            finished_day_index = finished.day_index
            await write_epilogue(session, finished)
        # 2. Дописываем эпилог отдельным корочким постом (итоги уже ушли без него).
        async with SessionLocal() as session:
            finished = await session.get(Round, finished_id)
            if finished is not None:
                await announce_epilogue(_bot, finished)
        # 3. Материализуем и открываем новый день. День рендерится сразу
        # целиком по известному итогу «вчера» — без заготовки из часа подсчёта.
        # Финализация открывает ровно день после закрытого (N+1), а не
        # latest+1: так тик, уже создавший N+1, не провоцирует эскалацию в N+2
        # (двойной день, потерянные итоги N+1).
        async with SessionLocal() as session:
            nxt, created = await create_next_round_detailed(
                session, base_day_index=finished_day_index
            )
        if created:
            # finished не передаём: итоги уже разосланы отдельным постом.
            await announce_new_day(_bot, nxt)
        # 4. Личные эха победителям — как и раньше, фоном после итогов.
        if settings.personal_echo:
            asyncio.create_task(_personal_echo_job(finished_id))
    except Exception:
        logger.exception("Доработка дня %s упала (итоги уже ушли отдельно)", finished_id)


async def _payout_dispatch_job() -> None:
    """Немедленная отправка вознаграждений после вскрытия итогов."""
    try:
        from app.ton_pay import dispatch_pending_payouts

        sent = await dispatch_pending_payouts(bot=_bot)
        logger.info("Диспетчер выплат (kick): отправлено %d", sent)
    except Exception:
        logger.exception("Kick выплат не удался (ретраи продолжатся по расписанию)")


async def _watch_job() -> None:
    """Watcher ставок с ботом: игрок получает личное о судьбе перевода."""
    from app.ton_watch import watch_once

    await watch_once(bot=_bot)


async def _ton_maintenance() -> None:
    """Финализация дней, очередь выплат, ретраи, копилки недели и месяца."""
    from app.leaderboard import settle_month_if_due, settle_week_if_due
    from app.ops import check_anomalies
    from app.ton_pay import settle_closed_rounds

    await settle_closed_rounds(bot=_bot)
    await settle_week_if_due(bot=_bot)
    await settle_month_if_due(bot=_bot)
    try:
        problems = await check_anomalies(_bot)
        if problems:
            logger.warning("Аномалии: %s", "; ".join(problems))
    except Exception:
        logger.exception("Проверка аномалий упала (не мешает обслуживанию)")


async def boot_maintenance() -> None:
    """Разовые задачи при старте: свежий бэкап БД до всего остального."""
    from app.backups import backup_job

    await backup_job()


def _register_job(job_id: str, func, trigger: str, **kwargs) -> None:
    """Регистрация джобы с изоляцией сбоев.

    Инцидент: обязательный аргумент bot в одной джобе ронял всю
    start_scheduler() — игра оставалась без тиков, watcher'а и выплат,
    а вебхук продолжал отвечать, маскируя мёртвое расписание. Теперь
    кривая регистрация глушит только саму себя.
    """
    try:
        scheduler.add_job(
            func,
            trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            **kwargs,
        )
    except Exception:
        logger.exception("Джоба %s не зарегистрирована", job_id)


def shutdown_scheduler() -> None:
    """Остановка без AttributeError, если планировщик так и не стартовал."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


def start_scheduler() -> None:
    from app.backups import backup_job

    _register_job("way-tick", tick, "interval", seconds=15)
    # Суточный бэкап в «мёртвый» час: 04:17 UTC.
    _register_job("db-backup", backup_job, "cron", hour=4, minute=17)
    if settings.ton_enabled:
        _register_job("ton-watch", _watch_job, "interval", seconds=60)
        _register_job("ton-settle", _ton_maintenance, "interval", seconds=120)
    # Шлифовка картинок-заглушек: каждые 2 часа, окно 24 часа с момента дня.
    from app.rounds import polish_stub_images

    _register_job("img-polish", polish_stub_images, "interval", hours=2)
    # Напоминание о голосовании: каждый час с 11:00 до 22:00 UTC
    _register_job("vote-reminder", _vote_reminder_job, "cron", hour="11-22", minute=30)
    # Еженедельная L2-вычитка стиля: воскресенье 18:00 UTC, отчёт админам.
    from app.style_review import run_weekly_review_and_notify

    _register_job(
        "style-review",
        run_weekly_review_and_notify,
        "cron",
        day_of_week="sun",
        hour=18,
        minute=0,
    )
    # Еженедельный отчёт стаи: воскресенье 20:00 UTC
    _register_job(
        "weekly-report",
        _weekly_report_job,
        "cron",
        day_of_week="sun",
        hour=20,
        minute=0,
    )
    # GEPA: еженедельная эволюция промпт-генов: воскресенье 21:00 UTC
    _register_job(
        "gepa-evolution",
        _gepa_evolution_job,
        "cron",
        day_of_week="sun",
        hour=21,
        minute=0,
    )
    scheduler.start()


async def _vote_reminder_job() -> None:
    """Напоминание игрокам проголосовать: DM тем, кто ещё не голосовал сегодня."""
    from app.handlers.common import bot_instance

    bot = bot_instance
    if bot is None:
        return
    try:
        async with SessionLocal() as session:
            current = await get_active_round(session)
            if current is None or current.status != RoundStatus.OPEN:
                return
            # Получаем всех игроков, которые ещё не голосовали
            from sqlalchemy import select as _select

            from app.models import Vote

            voted_result = await session.execute(
                _select(Vote.player_id).where(Vote.round_id == current.id)
            )
            voted_ids = {row[0] for row in voted_result.all()}

            from app.models import Player

            all_players = await session.execute(
                _select(Player).where(Player.dm_subscribed == True)
            )
            unbotted = [p for p in all_players.scalars().all() if p.id not in voted_ids]

            if not unbotted:
                return

            law_name = {
                "MAJORITY": "Большинство",
                "MINORITY": "Меньшинство",
                "MEDIAN": "Медиана",
            }.get(current.win_rule.value, "???")

            text = (
                f"🐺 День {current.day_index} открыт. Тропа ждёт.\n"
                f"⚖️ Закон: {law_name}\n"
                f"📖 {current.chapter_title}\n\n"
                f"Голосование закрывается скоро. Выбери тропу."
            )

            from app.broadcast import _dm_send_all

            async def _deliver(pid: int) -> None:
                try:
                    await bot.send_message(pid, text)
                except Exception:
                    pass

            sent = await _dm_send_all(bot, _deliver, "vote-reminder")
            logger.info("Напоминание о голосовании отправлено: %d сообщений", sent)
    except Exception as exc:
        logger.warning("Ошибка напоминания о голосовании: %s", exc)


async def _weekly_report_job() -> None:
    """Еженедельный отчёт стаи: статистика + топ стриков."""
    from app.handlers.common import bot_instance
    from app.streaks import weekly_report

    bot = bot_instance
    if bot is None:
        return
    try:
        async with SessionLocal() as session:
            report = await weekly_report(session)

        from app.broadcast import _dm_send_all

        async def _deliver(pid: int) -> None:
            try:
                await bot.send_message(pid, report)
            except Exception:
                pass

        sent = await _dm_send_all(bot, _deliver, "weekly-report")
        logger.info("Еженедельный отчёт отправлен: %d сообщений", sent)
    except Exception as exc:
        logger.warning("Ошибка еженедельного отчёта: %s", exc)


async def _gepa_evolution_job() -> None:
    """GEPA: еженедельная эволюция промпт-генов.
    
    Собирает fitness-данные за неделю (энтропия, биграммы, голосование, стрики),
    оценивает популяцию, запускает эволюцию, сохраняет лучший ген в watcher_state.
    """
    from app.models import StoryBeat, WatcherState
    from app.narrative_ai import GEPAPopulation, text_entropy, bigram_diversity
    from sqlalchemy import select as _select

    try:
        async with SessionLocal() as session:
            # Собираем данные за последние 7 дней
            beats = (
                await session.execute(
                    _select(StoryBeat.winning_text)
                    .order_by(StoryBeat.day_index.desc())
                    .limit(7)
                )
            ).all()

            if len(beats) < 3:
                logger.info("GEPA: недостаточно данных для эволюции (%d дней)", len(beats))
                return

            # Считаем средние метрики
            entropies = []
            bigrams = []
            for (text,) in beats:
                if text and len(text.split()) > 20:
                    entropies.append(text_entropy(text))
                    bigrams.append(bigram_diversity(text))

            avg_entropy = sum(entropies) / max(len(entropies), 1)
            avg_bigram = sum(bigrams) / max(len(bigrams), 1)

            # Загружаем популяцию из watcher_state
            ws_result = await session.execute(
                _select(WatcherState).where(WatcherState.key == "gepa_population")
            )
            ws_row = ws_result.scalar_one_or_none()
            if ws_row and ws_row.value:
                pop = GEPAPopulation.from_json(ws_row.value)
            else:
                pop = GEPAPopulation()

            # Fitness (без данных голосования — используем только качество текста)
            pop.evaluate_fitness(
                week_entropy=avg_entropy,
                week_bigram=avg_bigram,
                week_vote_rate=0.5,  # нейтральная оценка без данных
                week_streak_rate=0.5,
            )

            # Эволюция
            pop.evolve()

            # Сохраняем
            if ws_row is None:
                ws_row = WatcherState(key="gepa_population", value="")
                session.add(ws_row)
            ws_row.value = pop.to_json()
            await session.commit()

            best = pop.best_gene()
            logger.info(
                "GEPA gen %d: best=%.3f tone='%s' sensory='%s' pacing='%s'",
                pop.generation, best.fitness,
                best.system_tone, best.sensory_emphasis, best.pacing_style,
            )
    except Exception as exc:
        logger.warning("GEPA эволюция не удалась: %s", exc)


def get_active_gepa_gene_sync() -> "PromptGene | None":
    """Синхронно получает лучший ген GEPA из watcher_state (для prompt building).
    Вызывается из story.py при построении промпта."""
    from app.models import WatcherState
    from app.narrative_ai import GEPAPopulation
    from sqlalchemy import select as _select

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return None  # не блокируем event loop

        async def _load():
            async with SessionLocal() as session:
                result = await session.execute(
                    _select(WatcherState).where(WatcherState.key == "gepa_population")
                )
                ws = result.scalar_one_or_none()
                if ws and ws.value:
                    pop = GEPAPopulation.from_json(ws.value)
                    return pop.best_gene()
                return None

        return loop.run_until_complete(_load())
    except Exception:
        return None
