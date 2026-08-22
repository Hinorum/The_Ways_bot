"""Исходящие выплаты победителям: расчёт в stakes.finalize_day_payouts,
отправка здесь — через pytoniq (кошелёк казначея WalletV4R2 напрямую к
лайтсерверам активной сети).

Жизненный цикл выплаты: pending → sending (зафиксировано ДО вещания, чтобы
падение сервиса не привело к двойной отправке) → sent / failed. Зависшие
sending и неуспешные failed с attempts < PAYOUT_MAX_ATTEMPTS оживают каждый
цикл автоматически; при исчерпании лимита админ получает алерт.

tx_hash после отправки — метка вещания «bcast:<unix>»: лайтсервер не
возвращает хеш транзакции. Фактический перевод ищется в эксплорере по адресу
казначея и memo-комментарию вида way:<день>:<тип>#<id выплаты>.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from sqlalchemy import func, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Round, RoundStatus
from app.stakes import finalize_day_payouts

logger = logging.getLogger(__name__)

# Синглтон кошелька казначея: подключение к лайтсерверам дорогое, держим одно.
_wallet_lock = asyncio.Lock()
_provider = None
_wallet = None
_wallet_network: str | None = None


async def pending_payout_count(session) -> int:
    """Сколько переводов ещё не ушли (обе сети, включая dead-letter failed).

    «sent» — единственное конечное состояние: всё остальное значит, что
    деньги игроку ещё должны. Сброс игры обязан ждать, пока долг закрыт.
    """
    result = await session.execute(
        select(func.count()).select_from(Payout).where(Payout.status != "sent")
    )
    return int(result.scalar_one())


async def _get_wallet():
    """Ленивая инициализация WalletV4R2 казначея для активной сети."""
    global _provider, _wallet, _wallet_network
    network = "testnet" if settings.is_testnet else "mainnet"
    async with _wallet_lock:
        if _wallet is not None and _wallet_network == network:
            return _wallet
        if _provider is not None:
            try:
                await _provider.close_all()
            except Exception:
                pass
            _provider = None
            _wallet = None
        from pytoniq import LiteBalancer, WalletV4R2

        if network == "testnet":
            _provider = LiteBalancer.from_testnet_config()
        else:
            _provider = LiteBalancer.from_mainnet_config()
        await _provider.start_up()
        words = settings.active_treasury_mnemonic.replace("\n", " ").split()
        if len(words) < 12:
            raise ValueError("Мнемоника казначея неполная (нужно 24 слова)")
        _wallet = await WalletV4R2.from_mnemonic(_provider, words)
        _wallet_network = network
        logger.info("Кошелёк казначея готов (%s)", network)
        return _wallet


def _comment_cell(text: str):
    from pytoniq_core import begin_cell

    return begin_cell().store_uint(0, 32).store_string(text[:120]).end_cell()


async def send_ton_transfer(dest_address: str, amount_nanotons: int, comment: str) -> str | None:
    """Отправляет перевод с казначея. Возвращает метку вещания или None.

    None значит «не ушло» — выплата вернётся в очередь ретраев. Успех
    фиксируется лайтсервером (результат 1); фактический хеш транзакции
    смотрится в эксплорере по memo-комментарию.
    """
    if not settings.ton_enabled or not settings.active_treasury_mnemonic:
        logger.warning("TON выключен или нет мнемоники: выплата к …%s не отправлена", dest_address[-6:])
        return None
    try:
        wallet = await _get_wallet()
        result = await wallet.transfer(
            destination=dest_address,
            amount=amount_nanotons,
            body=_comment_cell(comment),
        )
        if result != 1:
            logger.warning("Лайтсерверы не приняли перевод к …%s", dest_address[-6:])
            return None
        marker = f"bcast:{int(datetime.now(timezone.utc).timestamp())}"
        logger.info("Перевод %d нанотонов к …%s разослан (%s)", amount_nanotons, dest_address[-6:], comment[:40])
        return marker
    except Exception as exc:
        logger.warning("Отправка TON не удалась (%s): %s", dest_address[-6:], exc)
        return None


async def _reset_retriable(session, network: str) -> None:
    """Оживляем зависшие sending/failed, пока не исчерпан лимит попыток."""
    await session.execute(
        update(Payout)
        .where(
            Payout.status.in_(["failed", "sending"]),
            Payout.attempts < settings.payout_max_attempts,
            Payout.dest_address != "",
            Payout.network == network,
        )
        .values(status="pending")
    )


async def _alert_admin(bot: Bot | None, network: str) -> None:
    """Алерты о failed-выплатах. Дедуп — колонка payouts.alerted в БД:
    переживает рестарт и безопасен при нескольких инстансах."""
    if bot is None:
        return
    async with SessionLocal() as session:
        fresh = (
            (
                await session.execute(
                    select(Payout.id).where(
                        Payout.status == "failed",
                        Payout.alerted.is_(False),
                        Payout.network == network,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not fresh:
            return
        text = (
            f"⚠️ Выплаты не ушли ({len(fresh)} шт., сеть {network}). "
            f"Id: {', '.join(map(str, fresh[:10]))}{'…' if len(fresh) > 10 else ''}. "
            "Проверь казначея и SDK."
        )
        await session.execute(
            update(Payout)
            .where(Payout.id.in_(fresh))
            .values(alerted=True)
        )
        await session.commit()
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("Алерт админу %s не доставлен: %s", admin_id, exc)


async def dispatch_pending_payouts(limit: int = 50, bot: Bot | None = None) -> int:
    sent = 0
    network = "testnet" if settings.is_testnet else "mainnet"
    async with SessionLocal() as session:
        # Ретрай: зависшие failed с неисчерпанным лимитом снова в очередь.
        await _reset_retriable(session, network)
        await session.commit()
        result = await session.execute(
            select(Payout)
            .where(
                Payout.status == "pending",
                Payout.amount_nanotons > 0,
                Payout.network == network,
            )
            .order_by(Payout.id.asc())
            .limit(limit)
        )
        payouts = list(result.scalars().all())
        # Фиксируем «взятые в работу» ДО вещания: падение сервиса между
        # broadcast и коммитом не приведёт к повторной отправке.
        for payout in payouts:
            if payout.dest_address:
                payout.attempts += 1
                payout.status = "sending"
        await session.commit()
        for payout in payouts:
            if not payout.dest_address:
                payout.status = "failed"
                continue
            try:
                tx_hash = await send_ton_transfer(
                    payout.dest_address,
                    payout.amount_nanotons,
                    comment=f"way:{payout.round_id}:{payout.kind}#{payout.id}",
                )
            except Exception as exc:
                logger.warning("Выплата %s не ушла: %s", payout.id, exc)
                tx_hash = None
            if tx_hash:
                payout.tx_hash = tx_hash
                payout.status = "sent"
                payout.sent_at = datetime.now(timezone.utc)
                payout.attempts = 0
                sent += 1
            elif payout.attempts >= settings.payout_max_attempts:
                payout.status = "failed"
            else:
                # Лимит не исчерпан — вернётся в очередь следующего цикла.
                payout.status = "pending"
        await session.commit()
    dead = [p.id for p in payouts if p.status == "failed"]
    if dead:
        logger.warning("Выплаты окончательно не отправлены: %s", dead)
    # Алерт по ВСЕМ неотправленным без предупреждения (включая найденные
    # после рестарта): дедуп внутри _alert_admin по колонке alerted.
    await _alert_admin(bot, network)
    return sent


async def settle_closed_rounds(bot: Bot | None = None) -> int:
    """Финализирует фонды закрытых дней и разбирает очередь выплат."""
    async with SessionLocal() as session:
        result = await session.execute(
            select(Round.id).where(
                Round.status == RoundStatus.CLOSED,
                Round.payouts_finalized.is_(False),
            )
        )
        round_ids = [row[0] for row in result.all()]
    created = 0
    for round_id in round_ids:
        async with SessionLocal() as session:
            round_row = await session.get(Round, round_id)
            if round_row is not None:
                created += await finalize_day_payouts(session, round_row)
    await dispatch_pending_payouts(bot=bot)
    return created
