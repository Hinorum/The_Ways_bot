"""Сквозной прогон игрового контура на живом тестнете.

Ставки -> подсчёт дня -> выплаты -> зеркало казны. Скрипт дёргает РЕАЛЬНЫЕ
функции движка (watcher, жизненный цикл дня, финализацию, диспетчер выплат,
зеркало), кроме бота Telegram: рассылки и личные сообщения не задействованы
(bot=None везде). Цель — полный цикл «в железе» на testnet: ставка реальным
переводом приходит на казначея, день закрывается, приз уходит обратно,
зеркало сходится в ноль.

Безопасность: скрипт отказывается стартовать, пока окружение не выглядит как
выделенный тестнет-контур (TON_NETWORK=testnet, тестнет-мнемоники, адрес
владельца, игрок из E2E_PLAYER_ID с привязанным кошелём). Прогон против
mainnet или без полного набора ключей просто падает с перечнем причин.

Фазы (по одной или все подряд):

    python scripts/e2e_testnet.py check     # охранный гейт + диагностика казначея
    python scripts/e2e_testnet.py stake     # голос + реальная ставка с кошелька игрока
    python scripts/e2e_testnet.py close     # закрыть день: подсчёт, победитель, выплаты
    python scripts/e2e_testnet.py dispatch  # разобрать очередь выплат (реальные переводы)
    python scripts/e2e_testnet.py mirror    # синк зеркала и тождество баланса «в ноль»
    python scripts/e2e_testnet.py full      # все фазы по порядку (idempotent, перезапускаемо)

Окружение (в дополнение к обычному testnet-контуру):
    E2E_PLAYER_ID        — Telegram-идентификатор игрока (запись в players)
    E2E_PLAYER_MNEMONIC  — мнемоника кошелька ИГРОКА, привязанного к /wallet
    E2E_STAKE_TIMEOUT_SECONDS (опц.) — сколько ждать подтверждения ставки
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Запуск скрипта из любого каталога: скрипт ходит в app.*.
# isort: off
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# isort: on

logger = logging.getLogger("e2e_testnet")

_EXIT_OK = 0
_EXIT_GUARD = 2


def guard() -> list[str]:
    """Охранный гейт: не стартовать без выделенного testnet-контура.

    Читает переменные окружения НАПРЯМУЮ (без импорта app.*), поэтому вызов
    дешёвый и безопасный — годится и для юнит-теста гейта.
    """
    reasons: list[str] = []
    if os.getenv("TON_NETWORK", "mainnet").strip().lower() != "testnet":
        reasons.append("TON_NETWORK должен быть testnet (скрипт не предназначен для mainnet)")
    if not os.getenv("DATABASE_URL"):
        reasons.append("не задан DATABASE_URL (прогон идёт против живой БД тестнет-бота)")
    if not os.getenv("TREASURY_TESTNET_ADDRESS"):
        reasons.append("не задан TREASURY_TESTNET_ADDRESS (адрес казначея тестнет-контура)")
    if not os.getenv("TREASURY_TESTNET_MNEMONIC"):
        reasons.append("не задан TREASURY_TESTNET_MNEMONIC (мнемоника казначея тестнет-контура)")
    if not os.getenv("OWNER_WALLET_ADDRESS"):
        reasons.append("не задан OWNER_WALLET_ADDRESS (иначе доли казны некому выплачивать)")
    if not os.getenv("E2E_PLAYER_ID"):
        reasons.append("не задан E2E_PLAYER_ID (Telegram-идентификатор игрока в players)")
    if not os.getenv("E2E_PLAYER_MNEMONIC"):
        reasons.append("не задана E2E_PLAYER_MNEMONIC (кошелёк игрока, привязанный к /wallet)")
    return reasons


def _player_id() -> int:
    return int(os.environ["E2E_PLAYER_ID"])


def _require_player_env() -> None:
    if "E2E_PLAYER_MNEMONIC" not in os.environ:
        raise RuntimeError("Нет E2E_PLAYER_MNEMONIC: охранный гейт должен был остановить прогон")


def _stake_wait_seconds() -> int:
    raw = os.getenv("E2E_STAKE_TIMEOUT_SECONDS", "")
    return int(raw) if raw.isdigit() else 180


async def _treasury_address() -> str:
    from app.config import settings

    if not settings.active_treasury_address:
        raise RuntimeError("Нет адреса казначея для активной сети (TREASURY_TESTNET_ADDRESS?)")
    return settings.active_treasury_address


async def _player_provider():
    """Лайтсерверный провайдер для кошелька игрока (тот же источник, что казначей)."""
    from pytoniq import LiteBalancer

    from app.config import settings
    from app.ton_pay import _fetch_remote_json

    if settings.liteserver_config_url:
        config = await _fetch_remote_json(settings.liteserver_config_url)
        provider = LiteBalancer.from_config(config)
        logger.info("Лайтсерверы игрока: конфиг из LITESERVER_CONFIG_URL")
    else:
        provider = LiteBalancer.from_testnet_config()
    await provider.start_up()
    return provider


async def _player_wallet(provider):
    """Кошелёк игрока из E2E_PLAYER_MNEMONIC; версия контракта — по привязанному адресу."""
    from pytoniq.contract.wallets.wallet import WalletV4R2
    from pytoniq.contract.wallets.wallet_v5 import WalletV5R1
    from pytoniq_core.crypto.keys import mnemonic_to_private_key, private_key_to_public_key

    from app.db import SessionLocal
    from app.models import Player
    from app.ton_pay import NETWORK_GLOBAL_IDS, WALLET_VERSIONS, _wallet_address
    from app.ton_utils import normalize_address

    words = os.environ["E2E_PLAYER_MNEMONIC"].replace("\n", " ").split()
    _, private_key = mnemonic_to_private_key(words)
    public_key = private_key_to_public_key(private_key)
    gid = NETWORK_GLOBAL_IDS["testnet"]

    async with SessionLocal() as session:
        player = await session.get(Player, _player_id())
    bound = normalize_address(player.wallet_address) if player and player.wallet_address else ""

    version: str | None = None
    for candidate in WALLET_VERSIONS:
        derived = normalize_address(_wallet_address(candidate, public_key, gid))
        if derived == bound:
            version = candidate
            break
    if version is None:
        raise RuntimeError(
            "Кошелёк из E2E_PLAYER_MNEMONIC не совпадает с привязанным адресом игрока"
            f" (привязан: {bound or '<нет>'}) — проверь мнемонику"
        )

    if version == "v5r1":
        wallet = await WalletV5R1.from_private_key(provider, private_key=private_key, wc=0, network_global_id=gid)
    else:
        wallet = await WalletV4R2.from_private_key(provider, private_key, wc=0)
    logger.info("Кошелёк игрока готов (%s, контракт %s)", wallet.address.to_str(), version)
    return wallet


def _comment_cell(text: str):
    from pytoniq_core import begin_cell

    return begin_cell().store_uint(0, 32).store_string(text[:120]).end_cell()


async def phase_check() -> int:
    """Охранный гейт + диагностика казначея (updates ничего не отправляет)."""
    from app.config import settings
    from app.ton_pay import treasury_diagnostics

    reasons = guard()
    if reasons:
        logger.error("Скрипт не стартует — окружение не похоже на выделенный тестнет:")
        for reason in reasons:
            logger.error("  - %s", reason)
        return _EXIT_GUARD

    logger.info("Контур: TON_NETWORK=%s, казначей …%s", settings.is_testnet, settings.active_treasury_address[-6:])
    logger.info("%s", await treasury_diagnostics())
    return _EXIT_OK


async def _load_open_money_round():
    """Игрок, открытый денежный день и его голос (без сети)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Player, RoundStatus
    from app.ops import is_game_paused
    from app.rounds.queries import get_active_round
    from app.stakes import current_network

    async with SessionLocal() as session:
        if await is_game_paused(session):
            raise RuntimeError("Игра на паузе — стоп-кран должен быть открыт для ставки")
        player = await session.get(Player, _player_id())
        if player is None:
            raise RuntimeError(f"Игрока {_player_id()} нет в players")
        if not player.wallet_verified:
            raise RuntimeError("Кошелёк игрока не верифицирован (wallet_verified=false)")
        round_row = await get_active_round(session)
        if round_row is None or round_row.status != RoundStatus.OPEN:
            raise RuntimeError("Нет открытого дня: сначала запусти бота / создай день")
        if not round_row.money_mode:
            raise RuntimeError("Открытый день живёт без ставок (money_mode=false) — включи режим и открой день заново")
        amount = None
        if round_row is not None:
            from app.models import Stake

            existing = await session.execute(
                select(Stake).where(
                    Stake.round_id == round_row.id,
                    Stake.player_id == player.id,
                    Stake.network == current_network(),
                )
            )
            stake = existing.scalar_one_or_none()
            if stake is not None and stake.status in ("confirmed", "pending"):
                amount = stake.amount_nanotons
                position = await _existing_position(session, round_row.id, player.id)
                return player, round_row, position, stake.status, amount
        return player, round_row, None, None, None


async def _existing_position(session, round_id: int, player_id: int) -> int | None:
    from sqlalchemy import select

    from app.models import Vote

    row = await session.execute(
        select(Vote.card_position).where(Vote.round_id == round_id, Vote.player_id == player_id)
    )
    return row.scalar_one_or_none()


async def _has_confirmed_stake(session, player_id: int, round_id: int) -> bool:
    from sqlalchemy import select

    from app.models import Stake
    from app.stakes import current_network

    row = await session.execute(
        select(Stake.id).where(
            Stake.round_id == round_id,
            Stake.player_id == player_id,
            Stake.network == current_network(),
            Stake.status == "confirmed",
        )
    )
    return row.scalar_one_or_none() is not None


async def phase_stake() -> int:
    """Голос за путь + реальная ставка переводом с кошелька игрока."""
    from app.config import settings
    from app.db import SessionLocal
    from app.ton_utils import from_nano, to_nano
    from app.voting import cast_vote

    player, round_row, position, existing_status, existing_amount = await _load_open_money_round()
    if existing_status is not None:
        logger.info(
            "Ставка на день %s уже есть (%s, %.4f Gram) — отправку пропускаю",
            round_row.day_index,
            existing_status,
            from_nano(existing_amount),
        )
        return _EXIT_OK

    chosen = 0 if position is None else position
    async with SessionLocal() as session:
        status = await cast_vote(session, round_row, player.id, chosen)
        if status not in ("ok", "already"):
            raise RuntimeError(f"Голос за путь не прошёл: {status}")
        await session.commit()
    logger.info("Путь выбран: %d (день %s)", chosen, round_row.day_index)

    provider = await _player_provider()
    try:
        wallet = await _player_wallet(provider)
        amount = to_nano(settings.stake_min_ton)
        comment = f"e2e:день{round_row.day_index}"
        logger.info(
            "Отправляю %.4f Gram на казначея (комментарий '%s')…",
            from_nano(amount),
            comment,
        )
        result = await wallet.transfer(
            destination=await _treasury_address(),
            amount=amount,
            body=_comment_cell(comment),
        )
        if result != 1:
            raise RuntimeError(f"Лайтсерверы не приняли ставку (результат {result})")
    finally:
        try:
            await provider.close_all()
        except Exception:
            logger.warning("Не удалось закрыть провайдер игрока", exc_info=True)

    wait = settings.stake_confirm_seconds + 15
    logger.info("Ставка разослана. Жду %s с до подтверждения и цикла watcher…", wait)
    time.sleep(wait)

    from app.ton_watch import confirm_aged_pending, watch_once

    deadline = time.monotonic() + _stake_wait_seconds()
    found = False
    while time.monotonic() < deadline:
        try:
            await watch_once()
        except Exception as exc:
            logger.warning("Цикл watcher упал (переживаем): %s", exc)
        try:
            await confirm_aged_pending()
        except Exception:
            logger.exception("confirm_aged_pending упал (не мешает циклу)")
        async with SessionLocal() as session:
            if await _has_confirmed_stake(session, player.id, round_row.id):
                found = True
                break
        interval = max(15, int(getattr(settings, "ton_watch_interval_seconds", 30) or 30))
        await asyncio.sleep(interval)

    if not found:
        raise RuntimeError(
            f"Ставка не подтвердилась за {_stake_wait_seconds()} с — проверь перевод игрока в эксплорере"
        )
    logger.info("Ставка дня %s подтверждена.", round_row.day_index)
    return _EXIT_OK


async def phase_close() -> int:
    """Закрыть день: подсчёт, победитель, очередь выплат (без отправки)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Payout, RoundStatus
    from app.rounds.lifecycle import close_voting, finish_tally
    from app.rounds.queries import get_active_round
    from app.stakes import finalize_day_payouts

    async with SessionLocal() as session:
        round_row = await get_active_round(session)
        if round_row is None:
            raise RuntimeError("Нет активного дня — закрывать нечего")
        if round_row.status == RoundStatus.OPEN:
            await close_voting(session, round_row)
            await session.commit()
            logger.info("День %s закрыт на подсчёт (закон: %s)", round_row.day_index, round_row.win_rule.value)
        if round_row.status == RoundStatus.TALLYING:
            round_row, _ = await finish_tally(session, round_row)
            await session.commit()
        finalized = await finalize_day_payouts(session, round_row)
        await session.commit()

        rows = (await session.execute(select(Payout).where(Payout.round_id == round_row.id))).scalars().all()
        logger.info(
            "Итоги дня %s: победитель путь %s, финализировано выплат %s",
            round_row.day_index,
            round_row.winner_card,
            finalized,
        )
        for payout in rows:
            kind = payout.kind
            amount = (
                str(payout.amount_nanotons) if payout.kind == "refund" else f"{payout.amount_nanotons / 1e9:.4f} Gram"
            )
            logger.info(
                "  %s → %s: %s (%s)", kind, payout.player_id or "казна", amount, payout.dest_address or "без адреса"
            )
    return _EXIT_OK


async def phase_dispatch() -> int:
    """Разобрать очередь выплат: реальные исходящие переводы + повтор-проба против двойной отправки."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Payout
    from app.ton_pay import dispatch_pending_payouts

    async with SessionLocal() as session:
        pending = (
            (
                await session.execute(
                    select(Payout.id).where(Payout.kind.in_(("prize", "refund")), Payout.status == "pending")
                )
            )
            .scalars()
            .all()
        )
    logger.info("В очереди выплат: %s строк", len(pending))

    first = await dispatch_pending_payouts(bot=None)
    logger.info("Диспетчер разобрал %s переводов", first)

    again = await dispatch_pending_payouts(bot=None)
    if again:
        logger.warning("Второй цикл взял ещё %s переводов (проверь поток восполнения очереди)", again)
    logger.info("Повтор-проба анти-дубля завершена: %s переводов", again)
    return _EXIT_OK


async def phase_mirror() -> int:
    """Синк зеркала казны и тождество баланса «в ноль» с живой цепочкой."""
    from app.db import SessionLocal
    from app.ton_pay import fetch_account_state
    from app.treasury_mirror import mirror_balance, sync_treasury_mirror

    last: dict | None = None
    on_chain = 0
    for attempt in range(1, 5):
        try:
            last = await sync_treasury_mirror()
            if last:
                logger.info("Синк #%s: %s", attempt, _mirror_summary(last))
            async with SessionLocal() as session:
                mirrored = await mirror_balance(session, "testnet")
            on_chain_raw = await fetch_account_state()
            on_chain = on_chain_raw[0] or 0
            if on_chain_raw[2]:
                raise RuntimeError(f"Не удалось прочитать баланс казначея: {on_chain_raw[2]}")
            if mirrored == on_chain:
                break
            logger.warning("Зеркало ≠ цепочка: зеркало=%s, цепочка=%s (попытка %s/4)", mirrored, on_chain, attempt)
            await asyncio.sleep(20)
        except Exception as exc:
            if attempt == 4:
                raise
            logger.warning("Синк #%s упал (переживаем): %s", attempt, exc)
            await asyncio.sleep(20)

    async with SessionLocal() as session:
        mirrored = await mirror_balance(session, "testnet")
    if mirrored != on_chain:
        raise RuntimeError(f"Зеркало казны НЕ сходится в ноль: {mirrored} ≠ {on_chain} (network testnet)")
    logger.info("Зеркало казны сходится «в ноль»: %s нанотонов.", on_chain)
    return _EXIT_OK


def _mirror_summary(result: dict) -> str:
    try:
        return (
            f"баланс {result.get('balance', '?')}, bootstrapped={result.get('bootstrapped')}, "
            f"added={result.get('added')}, отчет={result.get('report', '')[:200]}"
        )
    except Exception:
        return str(result)[:200]


async def run_full() -> int:
    last = await phase_check()
    if last != _EXIT_OK:
        return last
    await phase_stake()
    await phase_close()
    await phase_dispatch()
    await phase_mirror()
    logger.info("Полный цикл завершён успешно.")
    return _EXIT_OK


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Сквозной прогон игрового контура на живом тестнете")
    parser.add_argument(
        "phase",
        nargs="?",
        default="full",
        choices=["check", "stake", "close", "dispatch", "mirror", "full"],
    )
    args = parser.parse_args()

    phases = {
        "check": phase_check,
        "stake": phase_stake,
        "close": phase_close,
        "dispatch": phase_dispatch,
        "mirror": phase_mirror,
        "full": run_full,
    }
    try:
        return asyncio.run(phases[args.phase]())
    except Exception as exc:
        logger.error("e2e прерван: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
