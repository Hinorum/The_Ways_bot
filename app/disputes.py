"""Каркас разрешения споров: формальные жалобы на итоги дня (см. /dispute).

Жалоба — это запись-претензия игрока на конкретный день (или вообще на что
угодно), которую разбирает хранитель резолюцией resolved/rejected. Сам исход
дня НЕ переворачивается и выплаты победителям НЕ трогаются задним числом.
Компенсация по подтверждённой претензии — обычная выплата kind="dispute" в
общей очереди выплат: она уходит только по выстраданному пути (кошелёк игрока,
привязанный и проверенный), а движением денег занимается проверенный прогон
dispatch_pending_payouts. Так спор остаётся прозрачным аудитом, а не рычагом
пересмотра реальных переводов.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Dispute, Payout, Player
from app.ton_utils import from_nano, to_nano

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _network() -> str:
    return "testnet" if settings.is_testnet else "mainnet"


def _enabled() -> bool:
    return getattr(settings, "disputes_enabled", True)


async def open_dispute(
    session: AsyncSession, round_id: int | None, player_id: int | None, reason: str
) -> str:
    """Открыть спор. Ничего не двигает: только запись-претензия."""
    if not _enabled():
        return "рассмотрение споров сейчас выключено"
    if player_id is None:
        return "укажи игрока (игровой id или @ник)"
    reason = (reason or "").strip()[:500]
    if not reason:
        return "укажи причину спора"
    session.add(Dispute(round_id=round_id, player_id=player_id, reason=reason))
    await session.commit()
    return "спор открыт и ждёт рассмотрения хранителем"


async def resolve_dispute(session: AsyncSession, dispute_id: int, note: str = "") -> str:
    return await _settle(session, dispute_id, "resolved", note)


async def reject_dispute(session: AsyncSession, dispute_id: int, note: str = "") -> str:
    return await _settle(session, dispute_id, "rejected", note)


async def _settle(
    session: AsyncSession, dispute_id: int, status: str, note: str
) -> str:
    d = await session.get(Dispute, dispute_id)
    if d is None:
        return "нет такого спора"
    if d.status != "open":
        return f"спор уже закрыт ({d.status})"
    d.status = status
    d.keeper_note = (note or "").strip()[:300]
    d.resolved_at = _now()
    await session.commit()
    return f"спор #{d.id} {status}"


async def compensate_dispute(
    session: AsyncSession, dispute_id: int, amount_gram, note: str = ""
) -> str:
    """Компенсация по спору: обычная выплата kind='dispute' в общей очереди.

    Только если у игрока привязан кошелёк; идёт в существующий прогон выплат.
    """
    if not _enabled():
        return "рассмотрение споров сейчас выключено"
    d = await session.get(Dispute, dispute_id)
    if d is None:
        return "нет такого спора"
    if d.status != "open":
        return f"спор уже закрыт ({d.status}) — компенсация невозможна"
    if d.player_id is None:
        return "у спора нет игрока — компенсировать некому"
    player = await session.get(Player, d.player_id)
    if player is None or not player.wallet_address:
        return "у игрока не привязан кошелёк — компенсация невозможна"
    try:
        amount = to_nano(float(str(amount_gram).replace(",", ".")))
    except (ValueError, TypeError):
        return "сумма должна быть числом в Gram"
    if amount <= 0:
        return "сумма должна быть положительной"
    max_payout = getattr(settings, "max_payout_gram", 1000)
    if from_nano(amount) > max_payout:
        return f"сумма {from_nano(amount):.4g} Gram превышает лимит {max_payout} Gram — проверь число"
    session.add(
        Payout(
            round_id=d.round_id,
            player_id=d.player_id,
            kind="dispute",
            amount_nanotons=amount,
            dest_address=player.wallet_address,
            network=_network(),
        )
    )
    if d.status == "open":
        d.status = "resolved"
        d.keeper_note = (note or "").strip()[:300] or "компенсация выдана"
        d.resolved_at = _now()
    await session.commit()
    return f"компенсация {from_nano(amount):.4g} Gram поставлена в очередь (спор #{d.id})"


async def open_disputes(session: AsyncSession, limit: int = 30) -> list[Dispute]:
    rows = await session.execute(
        select(Dispute)
        .where(Dispute.status == "open")
        .order_by(Dispute.id.asc())
        .limit(limit)
    )
    return list(rows.scalars().all())
