"""Наблюдатель входящих переводов казначейского кошелька (заготовка).

Раз в минуту забирает свежие транзакции казначея через TonAPI v2, сопоставляет
отправителя с привязанным кошельком игрока и регистрирует ставку на открытый
день. Переводы, которые не могут стать ставкой (неопознанный отправитель,
повтор за уже поставившего, закрытый день, слишком мелкая оплата смены пути),
автоматически попадают в очередь возвратов — деньги не оседают в казнее молча.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Income, Payout, Player, RevoteGrant, Round, RoundStatus, Stake, WatcherState
from app.payments import parse_revote_memo
from app.stakes import confirm_stake, current_network, register_stake
from app.ton_utils import from_nano, normalize_address, to_nano

logger = logging.getLogger(__name__)

CURSOR_KEY = "ton_watch_cursor_utime"
# Сердцебиение: отметка времени последнего УСПЕШНОГО цикла (пусть и без
# переводов). Курсор для этого не годится: он двигается только переводами,
# и на тихой цепочке честно «стареет», хотя watcher здоров.
BEAT_KEY = "ton_watch_beat_iso"
# Источник данных последнего успешного цикла («tonapi» / «toncenter»):
# видно в /health как watcher_source — устойчивый фолбэк сигнализирует
# о деградации основного индексатора.
SOURCE_KEY = "ton_watch_last_source"
# Одноразовая миграция старых привязок из UQ/EQ-формы в канонический raw-hex.
WALLET_NORM_KEY = "wallet_norm_v1"
# Стартовый откат для первого запуска: не глубже полусуток.
_CURSOR_FALLBACK_HOURS = 12
# TonAPI v2 отдаёт страницы транзакций; идём вглубь, пока не накроем курсор
# или не упрёмся в пустое место (история кончилась / подряд пустые страницы).
_PAGE_LIMIT = max(1, settings.watch_page_limit)
_MAX_PAGES = max(1, settings.watch_max_pages)
_EMPTY_STOP = 2


@dataclass(frozen=True)
class Transfer:
    tx_hash: str
    source: str
    value_nanotons: int
    comment: str
    utime: int
    # Пагинационный ключ провайдера (Toncenter v3 требует lt, TonAPI — хеш);
    # для TonAPI-переводов остаётся пустым.
    provider_ref: str = ""


def _api_headers(api_key: str) -> dict:
    return {"X-API-Key": api_key} if api_key else {}


async def fetch_recent_transfers(since_utime: int, before_hash: str | None = None) -> tuple[list[Transfer], bool]:
    """Страница входящих переводов казначея активной сети (новые сверху).

    Ошибки сети не поднимают исключение: возвращается (пусто, False), чтобы
    цикл знал, что проверка не состоялась, и не ставил сердцебиение.
    before_hash — пагинация вглубь.

    Честная работа с 404: раньше «нет истории» считалось здоровьем, и падение
    индексатора TonAPI маскировалось под тихую цепочку (реальный инцидент:
    ставки не находятся, а /health зелёный). Теперь 404 перепроверяется по
    /v2/accounts/{адрес}: если аккаунт активен и у него есть активность после
    курсора — история TonAPI врёт, цикл считается несостоявшимся (False),
    и _collect_transfers переключается на фолбэк Toncenter v3.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], True
    url = f"{settings.active_ton_api_base}/v2/accounts/{settings.active_treasury_address}/transactions"
    headers = _api_headers(settings.ton_api_key)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                params={"limit": _PAGE_LIMIT, "sort_order": "desc", **({"before": before_hash} if before_hash else {})},
                headers=headers,
            )
            if response.status_code == 404:
                # Пустая история бывает у двух причин: кошелёк правда молчал
                # или индексатор потерял историю. Различаем честно.
                return await _resolve_tonapi_empty_history(since_utime)
            response.raise_for_status()
            items = response.json().get("transactions", [])
    except Exception as exc:
        logger.warning("TonAPI недоступен: %s", exc)
        return [], False
    transfers: list[Transfer] = []
    for item in items:
        transfer = _parse_tx_item(item, since_utime)
        if transfer is not None:
            transfers.append(transfer)
    return transfers, True


async def _resolve_tonapi_empty_history(since_utime: int) -> tuple[list[Transfer], bool]:
    """404 истории транзакций: «правда пусто» или «индекс сломан»?

    Сверяемся с карточкой аккаунта: активный кошелёк с активностью после
    курсора при пустой истории — деградация индексатора. Не сумели проверить
    (сеть/не-200) — тоже считаем цикл несостоявшимся: лучше лишний проход
    через фолбэк, чем пропущенная ставка.
    """
    info = await _tonapi_account_info()
    if not isinstance(info, dict):
        logger.warning(
            "TonAPI отдал 404 истории транзакций, но карточка аккаунта недоступна — "
            "цикл не признаётся успешным, переводы пойдут через фолбэк"
        )
        return [], False
    status = str(info.get("status") or "").strip().lower()
    try:
        last_activity = int(info.get("last_activity") or 0)
    except (TypeError, ValueError):
        last_activity = 0
    if status == "active" and last_activity > since_utime:
        logger.warning(
            "TonAPI отдал 404 истории транзакций при активном казначее с активностью %s "
            "(курсор %s) — индекс истории деградировал",
            last_activity,
            since_utime,
        )
        return [], False
    return [], True


async def _tonapi_account_info() -> dict | None:
    """Карточка казначея в TonAPI (/v2/accounts/{адрес}) или None при сбое."""
    url = f"{settings.active_ton_api_base}/v2/accounts/{settings.active_treasury_address}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, headers=_api_headers(settings.ton_api_key))
    except Exception as exc:
        logger.warning("TonAPI не ответил на запрос карточки аккаунта: %s", exc)
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


# Jetton::transfer_notification — входящий токен (USDt, NOT, …), а не TON.
_JETTON_OPCODES = {"0x7362d09c"}
# Не спамим в лог одним и тем же токеном каждый минутный цикл.
_warned_jettons: set[str] = set()


def _is_jetton_notification(in_msg: dict) -> bool:
    opcode = str(in_msg.get("opcode") or "").strip().lower()
    if opcode in _JETTON_OPCODES:
        return True
    msg_data = in_msg.get("msg_data")
    if isinstance(msg_data, dict):
        decoded_op = str(msg_data.get("decoded_op") or "").strip().lower()
        if decoded_op == "transfer_notification":
            return True
    return False


def _parse_tx_item(item: dict, since_utime: int) -> Transfer | None:
    """Транзакция страницы -> Transfer либо None (старая/джеттон/мусор).

    Джеттон-уведомление — это НЕ ставка: value внутри обёртки — копейки
    газа, источник — jetton-кошелёк игрока. Такой перевод нельзя ни
    зачесть, ни автоматически вернуть, поэтому он пропускается целиком,
    без пыльного refund-payout: токены ждут ручного возврата с казначея.
    """
    try:
        in_msg = item.get("in_msg") or {}
        utime = int(item.get("utime", 0))
        if utime <= since_utime:
            return None
        if _is_jetton_notification(in_msg):
            tx_hash = str(item.get("hash") or "")
            if tx_hash and tx_hash not in _warned_jettons:
                if len(_warned_jettons) > 256:
                    _warned_jettons.clear()
                _warned_jettons.add(tx_hash)
                logger.warning(
                    "Входящий перевод %s… — токен (jetton), а не нативный Gram/TON. Ставкой не становится "
                    "и автоматически не возвращается: верни вручную с казначея.",
                    tx_hash[:16],
                )
            return None
        source = ((in_msg.get("source") or {}).get("address")) or ""
        value = int(in_msg.get("value") or 0)
        if value <= 0 or not source:
            return None
        return Transfer(
            tx_hash=_norm_tx_hash(str(item.get("hash") or "")),
            source=source,
            value_nanotons=value,
            comment=_decode_comment(in_msg),
            utime=utime,
        )
    except Exception as exc:
        logger.warning("Странная транзакция пропущена: %s", exc)
        return None


def _norm_tx_hash(raw: str) -> str:
    """Единая форма хеша транзакции для всех провайдеров — hex lowercase.

    TonAPI отдаёт base64url, Toncenter v3 — стандартный base64 с паддингом.
    Идемпотентность ставок/возвратов строится на tx_hash, поэтому одна и та же
    транзакция, увиденная разными источниками, обязана дать одну строку.
    Разобрать не удалось — возвращаем как есть (в нижнем регистре).
    """
    candidate = raw.strip()
    # Только правдоподобные длины хеша транзакции: hex-64 либо base64
    # тридцати двух байт (43 без паддинга / 44 с ним). Прочие строки —
    # служебные метки тестов и логов — проходят насквозь нетронутыми.
    if len(candidate) == 64:
        try:
            int(candidate, 16)
            return candidate.lower()
        except ValueError:
            pass
    if len(candidate) not in (43, 44):
        return candidate.lower()
    b64 = candidate.replace("-", "+").replace("_", "/")
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        return base64.b64decode(padded, validate=True).hex()
    except Exception:
        return candidate.lower()


def _parse_toncenter_item(item: dict, since_utime: int) -> Transfer | None:
    """Транзакция Toncenter v3 -> Transfer либо None (старая/пустая).

    Джеттон-уведомления в выборку по аккаунту казначея не попадают вовсе
    (они садятся на jetton-кошелёк отправителя), поэтому отдельного фильтра,
    как у TonAPI, здесь не нужно. Комментарий приходит декодированным в
    message_content.decoded с типом «comment».
    """
    try:
        in_msg = item.get("in_msg") or {}
        utime = int(item.get("now") or 0)
        if utime <= since_utime:
            return None
        source = in_msg.get("source") or ""
        if isinstance(source, dict):
            source = source.get("address") or ""
        value = int(str(in_msg.get("value") or 0))
        if value <= 0 or not source:
            return None
        decoded = (in_msg.get("message_content") or {}).get("decoded") or {}
        comment = ""
        if isinstance(decoded, dict) and decoded.get("@type") == "comment":
            comment = str(decoded.get("comment") or "")
        return Transfer(
            tx_hash=_norm_tx_hash(str(item.get("hash") or "")),
            source=str(source),
            value_nanotons=value,
            comment=comment,
            utime=utime,
            provider_ref=str(item.get("lt") or ""),
        )
    except Exception as exc:
        logger.warning("Странная транзакция Toncenter пропущена: %s", exc)
        return None


# Toncenter v3 не отдаёт страницы больше этого размера.
_TONCENTER_MAX_LIMIT = 256


async def _toncenter_page(since_utime: int, before_lt: str | None = None) -> tuple[list[Transfer], str]:
    """Страница переводов казначея через Toncenter API v3 (фолбэк TonAPI).

    Контракт как у fetch_recent_transfers, но вместо булева — состояние
    страницы (_PAGE_OK/_PAGE_DEGRADED): фолбэк вызывается, только когда
    основной источник деградировал. Пагинация вглубь по before_lt.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], _PAGE_OK
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/transactions"
    params: dict = {
        "account": settings.active_treasury_address,
        "limit": min(_PAGE_LIMIT, _TONCENTER_MAX_LIMIT),
        "sort": "desc",
    }
    if before_lt:
        params["before_lt"] = before_lt
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, params=params, headers=_api_headers(settings.toncenter_api_key))
            response.raise_for_status()
            items = response.json().get("transactions") or []
    except Exception as exc:
        logger.warning("Toncenter v3 недоступен: %s", exc)
        return [], _PAGE_DEGRADED
    transfers: list[Transfer] = []
    for item in items:
        transfer = _parse_toncenter_item(item, since_utime)
        if transfer is not None:
            transfers.append(transfer)
    return transfers, _PAGE_OK


def _decode_comment(in_msg: dict) -> str:
    raw = in_msg.get("raw_message") or ""
    if raw:
        return raw
    msg_data = in_msg.get("msg_data") or {}
    decoded = msg_data.get("decoded_comment") or ""
    if decoded:
        return decoded
    text_b64 = msg_data.get("text")
    if text_b64:
        try:
            return base64.b64decode(text_b64).decode("utf-8", "ignore")
        except Exception:
            return ""
    return ""


async def _stash_refund(session, transfer: Transfer, round_id: int | None) -> str:
    """Авто-возврат перевода, который не может стать ставкой или оплатой.

    Идемпотентно по tx_hash: повторная обработка той же транзакции не плодит
    вторую выплату. Отправка — обычным порядком через dispatch_pending_payouts.
    """
    duplicate = await session.execute(
        select(Payout.id).where(Payout.kind == "refund", Payout.tx_hash == transfer.tx_hash).limit(1)
    )
    if duplicate.scalar_one_or_none() is not None:
        return "refund_duplicated"
    session.add(
        Payout(
            round_id=round_id,
            player_id=None,
            kind="refund",
            amount_nanotons=transfer.value_nanotons,
            dest_address=transfer.source or "",
            tx_hash=transfer.tx_hash[:80],
            network=current_network(),
        )
    )
    await session.commit()
    logger.info("Перевод %s возвращён отправителю", transfer.tx_hash[:16])
    return "refund_queued"


async def _dm_stake(bot: Bot | None, player_id: int, text: str) -> None:
    """Личное сообщение о судьбе ставки; доставка не обязательна для учёта."""
    if bot is None or player_id <= 0:
        return
    try:
        await bot.send_message(player_id, text)
    except Exception as exc:
        logger.info("Сообщение игроку %s не доставлено: %s", player_id, exc)


async def process_transfer(transfer: Transfer, bot: Bot | None = None) -> str:
    """Сопоставляет перевод с игроком и открытым днём: ставка или оплата смены пути."""
    async with SessionLocal() as session:
        player_result = await session.execute(
            select(Player).where(Player.wallet_address == normalize_address(transfer.source))
        )
        player = player_result.scalar_one_or_none()
        if player is None:
            # Деньги от неопознанного кошелька нельзя оставить в казнее молча.
            return await _stash_refund(session, transfer, None)
        revote_round_id = parse_revote_memo(transfer.comment)
        if revote_round_id is not None:
            status = await _process_revote(session, transfer, player, revote_round_id)
            if status in ("revote_closed", "revote_too_small"):
                await _stash_refund(session, transfer, revote_round_id)
                await _dm_stake(
                    bot,
                    player.id,
                    f"↩️ Оплата {from_nano(transfer.value_nanotons):g} Gram возвращается: "
                    + ("день уже закрыт." if status == "revote_closed" else "сумма меньше нужной."),
                )
            return status
        round_result = await session.execute(
            select(Round)
            .where(Round.status == RoundStatus.OPEN)
            .order_by(Round.day_index.desc())
            .limit(1)
        )
        round_row = round_result.scalar_one_or_none()
        if round_row is None:
            return await _stash_refund(session, transfer, None)
        result = await register_stake(
            session,
            round_row,
            player,
            transfer.value_nanotons,
            transfer.tx_hash,
            memo=transfer.comment,
        )
        amount = f"{from_nano(transfer.value_nanotons):g}"
        if result in ("already_staked", "closed"):
            # Ставка-строка не создана: без авто-возврата перевод исчез бы из учёты.
            await _stash_refund(session, transfer, round_row.id)
            reason = "ставка на этот день уже есть" if result == "already_staked" else "день уже закрылся"
            await _dm_stake(bot, player.id, f"↩️ Перевод {amount} Gram возвращается: {reason}.")
        elif result == "too_small":
            await _dm_stake(
                bot,
                player.id,
                f"↩️ Ставка {amount} Gram не принята (меньше минимума) — вернём после закрытия дня.",
            )
        elif result == "ok":
            age = datetime.now(timezone.utc).timestamp() - transfer.utime
            if age >= settings.stake_confirm_seconds:
                if await confirm_stake(session, transfer.tx_hash):
                    await _dm_stake(
                        bot, player.id, f"✅ Ставка {amount} Gram на день {round_row.day_index} принята."
                    )
    return result


async def confirm_aged_pending(bot: Bot | None = None) -> int:
    """Свежие переводы на момент обработки младше порога и остаются pending.

    Этот проход подтверждает их, когда возраст уже точно больше
    stake_confirm_seconds, и сообщает игроку. Закрытые дни не трогаем:
    их pending-ставки финализация вернёт как «залипшие».
    """
    confirmed = 0
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        rows = (
            (await session.execute(select(Stake).where(Stake.status == "pending")))
            .scalars()
            .all()
        )
        for stake in rows:
            created = stake.created_at if stake.created_at.tzinfo else stake.created_at.replace(tzinfo=timezone.utc)
            if now - created < timedelta(seconds=settings.stake_confirm_seconds):
                continue
            round_row = await session.get(Round, stake.round_id)
            if round_row is None or round_row.status != RoundStatus.OPEN:
                continue
            stake.status = "confirmed"
            stake.confirmed_at = now
            confirmed += 1
            await _dm_stake(
                bot,
                stake.player_id,
                f"✅ Ставка {from_nano(stake.amount_nanotons):g} Gram на день {round_row.day_index} принята.",
            )
        if confirmed:
            await session.commit()
    return confirmed


async def _process_revote(session, transfer: Transfer, player: Player, round_id: int) -> str:
    duplicate = await session.execute(
        select(RevoteGrant.id).where(RevoteGrant.unit_ref == transfer.tx_hash)
    )
    if duplicate.scalar_one_or_none() is not None:
        return "duplicate_tx"
    round_row = await session.get(Round, round_id)
    if round_row is None or round_row.status != RoundStatus.OPEN:
        return "revote_closed"
    if transfer.value_nanotons < to_nano(settings.revote_ton):
        return "revote_too_small"
    session.add(
        RevoteGrant(
            round_id=round_id,
            player_id=player.id,
            source="ton",
            unit_ref=transfer.tx_hash,
        )
    )
    # Ledger доходов: revote-перевод — выручка казны, её надо сверять.
    session.add(
        Income(
            kind="ton",
            amount_nanotons=transfer.value_nanotons,
            round_id=round_id,
            player_id=player.id,
            unit_ref=transfer.tx_hash,
            note=f"rv:{round_id}",
        )
    )
    await session.commit()
    return "revote_ok"


async def _read_cursor(session) -> int:
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is not None and row.value.isdigit():
        return int(row.value)
    return int((datetime.now(timezone.utc) - timedelta(hours=_CURSOR_FALLBACK_HOURS)).timestamp())


async def _write_cursor(session, utime: int) -> None:
    row = await session.get(WatcherState, CURSOR_KEY)
    if row is None:
        session.add(WatcherState(key=CURSOR_KEY, value=str(utime)))
    else:
        row.value = str(utime)
    await session.commit()


async def _write_beat(session) -> None:
    """Сердцебиение успешного цикла — для алертов и /health."""
    row = await session.get(WatcherState, BEAT_KEY)
    stamp = datetime.now(timezone.utc).isoformat()
    if row is None:
        session.add(WatcherState(key=BEAT_KEY, value=stamp))
    else:
        row.value = stamp
    await session.commit()


async def _write_source(session, source: str) -> None:
    """Источник данных последнего успешного цикла (для /health)."""
    row = await session.get(WatcherState, SOURCE_KEY)
    if row is None:
        session.add(WatcherState(key=SOURCE_KEY, value=source))
    else:
        row.value = source
    await session.commit()


# Состояние страницы провайдера: доверенная или нет.
_PAGE_OK = "ok"
_PAGE_DEGRADED = "degraded"

# Предупреждение о деградации TonAPI — не чаще раза в 10 минут, чтобы
# минутный цикл наблюдателя не заваливал лог одним и тем же сообщением.
_FALLBACK_WARN_EVERY_SECONDS = 600.0
_last_fallback_warning_at = 0.0


def _warn_degraded_primary() -> None:
    global _last_fallback_warning_at
    now = time.monotonic()
    if now - _last_fallback_warning_at < _FALLBACK_WARN_EVERY_SECONDS:
        return
    _last_fallback_warning_at = now
    logger.warning(
        "TonAPI деградировал (ошибка сети или 404 истории при живом казначее) — "
        "переводы читаются через фолбэк Toncenter v3"
    )


async def _tonapi_page(since_utime: int, before_hash: str | None) -> tuple[list[Transfer], str]:
    """Адаптер основного источника под единый контракт (список, состояние)."""
    transfers, ok = await fetch_recent_transfers(since_utime, before_hash=before_hash)
    return transfers, (_PAGE_OK if ok else _PAGE_DEGRADED)


async def _deep_collect(fetch_page, cursor_of, since: int) -> tuple[list[Transfer], bool]:
    """Глубокий проход по страницам одного провайдера.

    Возвращает (переводы, прошёл_ли_проход_полностью). Ненадёжная страница
    обрывает проход: частичный результат сохраняется, но вызывающий обязан
    не считать такой цикл успешным. Уходим вглубь до _MAX_PAGES × _PAGE_LIMIT
    переводов; пустое место (история короче страницы или две страницы подряд
    без новых переводов) завершает проход досрочно. Курсор хранится в БД,
    поэтому покрытие кумулятивно: после простоя накопившийся хвост
    догоняется за несколько минут.
    """
    transfers: list[Transfer] = []
    seen: set[str] = set()
    before: str | None = None
    empty_pages = 0
    for _page in range(_MAX_PAGES):
        page, state = await fetch_page(since, before)
        if state != _PAGE_OK:
            return transfers, False
        fresh = [t for t in page if t.tx_hash and t.tx_hash not in seen]
        for item in fresh:
            seen.add(item.tx_hash)
        transfers.extend(fresh)
        if not page or len(page) < _PAGE_LIMIT:
            return transfers, True  # история кончилась — глубже пусто
        oldest = page[-1]
        if oldest.utime <= since:
            return transfers, True  # страница дотянулась до курсора
        if not fresh:
            empty_pages += 1
            if empty_pages >= _EMPTY_STOP:
                return transfers, True  # подряд страницы без новых переводов
        else:
            empty_pages = 0
        before = cursor_of(page)
        await asyncio.sleep(0.12)  # бережём лимиты API на глубоком проходе
    return transfers, True


def _merge_unique(batches: list[list[Transfer]]) -> list[Transfer]:
    """Слияние результатов источников без дублей, по возрастанию utime."""
    seen: set[str] = set()
    merged: list[Transfer] = []
    for batch in batches:
        for transfer in batch:
            if transfer.tx_hash and transfer.tx_hash not in seen:
                seen.add(transfer.tx_hash)
                merged.append(transfer)
    return sorted(merged, key=lambda item: item.utime)


async def _collect_transfers(since: int) -> tuple[list[Transfer], bool, str]:
    """Все переводы после курсора: основной источник + фолбэк Toncenter.

    Основной проход TonAPI'ем; если он не завершился полностью (сеть легла
    или индекс отдаёт 404 истории при живом кошельке) — тот же проход
    повторяется по Toncenter v3, результаты сливаются без дублей. Источник
    успешного прохода возвращается третьим значением для /health.
    """
    primary, primary_complete = await _deep_collect(
        _tonapi_page, lambda page: page[-1].tx_hash, since
    )
    if primary_complete:
        return primary, True, "tonapi"
    _warn_degraded_primary()
    fallback, fallback_complete = await _deep_collect(
        _toncenter_page, lambda page: page[-1].provider_ref or page[-1].tx_hash, since
    )
    merged = _merge_unique([primary, fallback])
    if fallback_complete:
        return merged, True, "toncenter"
    return merged, False, "none"


async def _migrate_wallet_formats(session) -> None:
    """Разовый перевод старых привязок UQ/EQ… в канонический raw-hex.

    До нормализации watcher не находил отправителя: TonAPI отдаёт raw, а в БД
    лежала дружественная строка. Флаг в WatcherState делает миграцию идемпотентной.
    """
    row = await session.get(WatcherState, WALLET_NORM_KEY)
    if row is not None:
        return
    result = await session.execute(select(Player).where(Player.wallet_address.is_not(None)))
    changed = 0
    for player in result.scalars():
        normalized = normalize_address(player.wallet_address)
        if normalized != player.wallet_address:
            player.wallet_address = normalized
            changed += 1
    session.add(WatcherState(key=WALLET_NORM_KEY, value="1"))
    await session.commit()
    if changed:
        logger.info("Нормализовано адресов кошельков: %d", changed)


async def watch_once(bot: Bot | None = None) -> None:
    async with SessionLocal() as session:
        await _migrate_wallet_formats(session)
        since = await _read_cursor(session)
    transfers, api_ok, source = await _collect_transfers(since)
    processed_through = since
    for transfer in transfers:  # по возрастанию utime — старые раньше новых
        try:
            status = await process_transfer(transfer, bot=bot)
            if status not in ("duplicate_tx", "refund_duplicated"):
                logger.info("Перевод %s: %s", transfer.tx_hash[:16], status)
        except Exception as exc:
            # Курсор дальше не двигаем: незакрытый перевод попадёт в следующий цикл.
            logger.warning("Перевод %s не обработан: %s", transfer.tx_hash[:16], exc)
            break
        processed_through = max(processed_through, transfer.utime)
    try:
        await confirm_aged_pending(bot)
    except Exception:
        logger.exception("Подтверждение отложенных ставок упало (не мешает циклу)")
    if processed_through > since or api_ok:
        async with SessionLocal() as session:
            if processed_through > since:
                await _write_cursor(session, processed_through)
            if api_ok:
                # Сердцебиение ставится каждым успешным циклом — даже без
                # переводов: тишина в цепочке это здоровье, а не простой.
                await _write_beat(session)
                await _write_source(session, source)
