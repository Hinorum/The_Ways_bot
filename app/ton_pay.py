"""Исходящие выплаты победителям: расчёт в stakes.finalize_day_payouts,
отправка здесь — через pytoniq напрямую к лайтсерверам активной сети.
Казначейский кошелёк поддерживается в двух версиях контракта — v4r2 и
v5r1 (кошельки нового поколения): версия детектируется автоматически по
адресу казначея либо задаётся явно переменной TREASURY_WALLET_VERSION.

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
from app.ton_utils import normalize_address

logger = logging.getLogger(__name__)

# Синглтон кошелька казначея: подключение к лайтсерверам дорогое, держим одно.
_wallet_lock = asyncio.Lock()
_provider = None
_wallet = None
_wallet_network: str | None = None

# Глобальный идентификатор сети (конфиг #19 блокчейна): входит в wallet_id
# контракта v5, поэтому с одной мнемоникой тестнет- и мейннет-v5-кошельки
# имеют разные адреса.
NETWORK_GLOBAL_IDS = {"mainnet": -239, "testnet": -3}
# Поддерживаемые версии контракта казначея.
WALLET_VERSIONS = ("v4r2", "v5r1")


def _wallet_address(version: str, public_key: bytes, network_global_id: int, wc: int = 0) -> str:
    """Адрес кошелька данной версии для ключа — чистая локальная математика.

    Адрес = хеш StateInit(code + data), сеть не нужна. Data-ячейка v4 сеть не
    задаёт (адрес одинаков в обеих сетях), у v5 network_global_id входит в
    wallet_id внутри data.
    """
    from pytoniq.contract.wallets.wallet import WALLET_V4_R2_CODE, WalletV4R2
    from pytoniq.contract.wallets.wallet_v5 import WALLET_V5_R1_CODE, WalletV5R1
    from pytoniq_core.tlb.account import StateInit

    if version == "v4r2":
        data = WalletV4R2.create_data_cell(public_key=public_key, wc=wc)
        code = WALLET_V4_R2_CODE
    elif version == "v5r1":
        data = WalletV5R1.create_data_cell(public_key=public_key, wc=wc, network_global_id=network_global_id)
        code = WALLET_V5_R1_CODE
    else:
        raise ValueError(f"Неизвестная версия кошелька казначея: {version}")
    state_init = StateInit(code=code, data=data)
    return f"{wc}:{state_init.serialize().hash.hex()}"


def _detect_wallet_version(
    public_key: bytes, treasury_address: str, network_global_id: int
) -> tuple[str | None, dict[str, str]]:
    """Версия кошелька, чей производный адрес совпал с настроенным.

    Возвращает (версия | None, {версия: адрес-кандидат}) — кандидаты идут в
    текст ошибки, чтобы расхождение мнемоники и адреса было видно сразу.
    """
    target = normalize_address(treasury_address)
    candidates = {
        version: _wallet_address(version, public_key, network_global_id)
        for version in WALLET_VERSIONS
    }
    for version, address in candidates.items():
        if normalize_address(address) == target:
            return version, candidates
    return None, candidates


async def pending_payout_count(session) -> int:
    """Сколько переводов ещё не ушли (обе сети, включая dead-letter failed).

    «sent» — единственное конечное состояние успеха; «dismissed» — ручной
    вердикт хранителя (спам-перевод с рекламой и т.п.), он деньгам игрокам
    не равен и сбросу не мешает. Всё остальное значит, что деньги игроку
    ещё должны: сброс игры обязан ждать, пока долг закрыт.
    """
    result = await session.execute(
        select(func.count()).select_from(Payout).where(Payout.status.notin_(["sent", "dismissed"]))
    )
    return int(result.scalar_one())


async def resolve_dead_payout(session, payout_id: int, action: str) -> str | None:
    """Ручной разбор проблемной выплаты хранителем.

    action="spam" — статус «dismissed»: пыльный спам-перевод с рекламой,
    возврат которого не нужен или невозможен. Выплата исчезает из очереди,
    алертов и перестаёт блокировать /resetgame. action="retry" — обратно в
    очередь с нулевым счётчиком попыток (настоящий долг игроку). Возвращает
    новый статус или None, если выплаты нет либо она уже отправлена.
    """
    payout = await session.get(Payout, payout_id)
    if payout is None or payout.status == "sent":
        return None
    if action == "spam":
        payout.status = "dismissed"
    elif action == "retry":
        payout.status = "pending"
        payout.attempts = 0
        payout.alerted = False
    else:
        return None
    await session.commit()
    logger.info("Выплата %d разобрана вручную: %s", payout_id, payout.status)
    return payout.status


async def _get_wallet():
    """Ленивая инициализация кошелька казначея для активной сети.

    Версия контракта — из TREASURY_WALLET_VERSION («auto» = детект по адресу).
    Проверка пары мнемоника/адрес выполняется ДО подключения к сети: если
    производный адрес не совпал, отправлять нельзя в принципе — падаем с
    внятной ошибкой, а не молчаливыми неудачными выплатами.
    """
    global _provider, _wallet, _wallet_network
    network = "testnet" if settings.is_testnet else "mainnet"
    async with _wallet_lock:
        if _wallet is not None and _wallet_network == network:
            return _wallet
        if not settings.active_treasury_mnemonic:
            raise ValueError("Нет мнемоники казначея для активной сети")
        if not settings.active_treasury_address:
            raise ValueError("Нет адреса казначея для активной сети")
        words = settings.active_treasury_mnemonic.replace("\n", " ").split()
        if len(words) < 12:
            raise ValueError("Мнемоника казначея неполная (нужно 24 слова)")

        from pytoniq import LiteBalancer
        from pytoniq.contract.wallets.wallet import WalletV4R2
        from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
        from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

        _, private_key = mnemonic_to_private_key(words)
        public_key = private_key_to_public_key(private_key)
        network_global_id = NETWORK_GLOBAL_IDS[network]

        requested = settings.treasury_wallet_version.strip().lower()
        if requested in WALLET_VERSIONS:
            derived = _wallet_address(requested, public_key, network_global_id)
            if normalize_address(derived) != normalize_address(settings.active_treasury_address):
                raise ValueError(
                    f"Адрес казначея не совпадает с производным от мнемоники "
                    f"(TREASURY_WALLET_VERSION={requested}): {derived}. "
                    "Проверь пару мнемоника/адрес или верни auto."
                )
            version = requested
        else:
            version, candidates = _detect_wallet_version(
                public_key, settings.active_treasury_address, network_global_id
            )
            if version is None:
                raise ValueError(
                    "Адрес казначея не совпадает ни с одной поддерживаемой версией "
                    f"кошелька для этой мнемоники: {candidates}. Проверь адрес и "
                    "мнемонику, либо задай TREASURY_WALLET_VERSION=v4r2|v5r1 явно."
                )

        if _provider is not None:
            try:
                await _provider.close_all()
            except Exception:
                pass
            _provider = None
            _wallet = None
        if network == "testnet":
            _provider = LiteBalancer.from_testnet_config()
        else:
            _provider = LiteBalancer.from_mainnet_config()
        await _provider.start_up()
        if version == "v5r1":
            _wallet = await WalletV5R1.from_private_key(
                _provider, private_key=private_key, wc=0, network_global_id=network_global_id
            )
        else:
            _wallet = await WalletV4R2.from_private_key(_provider, private_key, wc=0)
        _wallet_network = network
        logger.info("Кошелёк казначея готов (%s, контракт %s)", network, version)
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
