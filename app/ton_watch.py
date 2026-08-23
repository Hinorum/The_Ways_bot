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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Income, Payout, Player, RevoteGrant, Round, RoundStatus, WatcherState
from app.payments import parse_revote_memo
from app.stakes import confirm_stake, current_network, register_stake
from app.ton_utils import normalize_address, to_nano

logger = logging.getLogger(__name__)

CURSOR_KEY = "ton_watch_cursor_utime"
# Сердцебиение: отметка времени последнего УСПЕШНОГО цикла (пусть и без
# переводов). Курсор для этого не годится: он двигается только переводами,
# и на тихой цепочке честно «стареет», хотя watcher здоров.
BEAT_KEY = "ton_watch_beat_iso"
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


async def fetch_recent_transfers(since_utime: int, before_hash: str | None = None) -> tuple[list[Transfer], bool]:
    """Страница входящих переводов казначея активной сети (новые сверху).

    Ошибки сети не поднимают исключение: возвращается (пусто, False), чтобы
    цикл знал, что проверка не состоялась, и не ставил сердцебиение.
    before_hash — пагинация вглубь.
    """
    if not settings.ton_enabled or not settings.active_treasury_address:
        return [], True
    url = f"{settings.active_ton_api_base}/v2/accounts/{settings.active_treasury_address}/transactions"
    headers = {"X-API-Key": settings.ton_api_key} if settings.ton_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                params={"limit": _PAGE_LIMIT, "sort_order": "desc", **({"before": before_hash} if before_hash else {})},
                headers=headers,
            )
            response.raise_for_status()
            items = response.json().get("transactions", [])
    except Exception as exc:
        logger.warning("TonAPI недоступен: %s", exc)
        return [], False
    transfers: list[Transfer] = []
    for item in items:
        try:
            utime = int(item.get("utime", 0))
            if utime <= since_utime:
                continue
            in_msg = item.get("in_msg") or {}
            source = ((in_msg.get("source") or {}).get("address")) or ""
            value = int(in_msg.get("value") or 0)
            if value <= 0 or not source:
                continue
            transfers.append(
                Transfer(
                    tx_hash=item.get("hash", ""),
                    source=source,
                    value_nanotons=value,
                    comment=_decode_comment(in_msg),
                    utime=utime,
                )
            )
        except Exception as exc:
            logger.warning("Странная транзакция пропущена: %s", exc)
    return transfers, True


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


async def process_transfer(transfer: Transfer) -> str:
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
        if result in ("already_staked", "closed"):
            # Ставка-строка не создана: без авто-возврата перевод исчез бы из учёты.
            await _stash_refund(session, transfer, round_row.id)
        if result == "ok":
            age = datetime.now(timezone.utc).timestamp() - transfer.utime
            if age >= settings.stake_confirm_seconds:
                await confirm_stake(session, transfer.tx_hash)
    return result


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


async def _collect_transfers(since: int) -> tuple[list[Transfer], bool]:
    """Все переводы после курсора: уходим вглубь пагинацией по before-хешу.

    За цикл покрывается до _MAX_PAGES × _PAGE_LIMIT переводов (по умолчанию
    50 × 100 = 5000). Курсор хранится в БД, поэтому покрытие кумулятивно:
    после простоя накопившийся хвост догоняется за несколько минут. Пустое
    место (история короче страницы или две страницы подряд без новых
    переводов) завершает проход досрочно — без лишних запросов вглубь.
    Второе значение — прошла ли хотя бы одна страница успешно: если TonAPI
    лежал весь цикл, сердцебиение ставить нельзя (проверки не было).
    """
    transfers: list[Transfer] = []
    seen: set[str] = set()
    before: str | None = None
    empty_pages = 0
    api_ok = False
    for _page in range(_MAX_PAGES):
        page, page_ok = await fetch_recent_transfers(since, before_hash=before)
        if not page_ok:
            return transfers, api_ok
        api_ok = True
        fresh = [t for t in page if t.tx_hash and t.tx_hash not in seen]
        for item in fresh:
            seen.add(item.tx_hash)
        transfers.extend(fresh)
        if not page or len(page) < _PAGE_LIMIT:
            break  # история кончилась — глубже пусто
        oldest = page[-1]
        if oldest.utime <= since:
            break  # страница дотянулась до курсора — глубже искать нечего
        if not fresh:
            empty_pages += 1
            if empty_pages >= _EMPTY_STOP:
                break  # подряд идущие страницы без новых переводов
        else:
            empty_pages = 0
        before = oldest.tx_hash
        await asyncio.sleep(0.12)  # бережём лимиты TonAPI на глубоком проходе
    return sorted(transfers, key=lambda item: item.utime), api_ok


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


async def watch_once() -> None:
    async with SessionLocal() as session:
        await _migrate_wallet_formats(session)
        since = await _read_cursor(session)
    transfers, api_ok = await _collect_transfers(since)
    processed_through = since
    for transfer in transfers:  # по возрастанию utime — старые раньше новых
        try:
            status = await process_transfer(transfer)
            if status not in ("duplicate_tx", "refund_duplicated"):
                logger.info("Перевод %s: %s", transfer.tx_hash[:16], status)
        except Exception as exc:
            # Курсор дальше не двигаем: незакрытый перевод попадёт в следующий цикл.
            logger.warning("Перевод %s не обработан: %s", transfer.tx_hash[:16], exc)
            break
        processed_through = max(processed_through, transfer.utime)
    if processed_through > since or api_ok:
        async with SessionLocal() as session:
            if processed_through > since:
                await _write_cursor(session, processed_through)
            if api_ok:
                # Сердцебиение ставится каждым успешным циклом — даже без
                # переводов: тишина в цепочке это здоровье, а не простой.
                await _write_beat(session)
