"""Ставки TON на путь: приём, фонд дня и распределение выплат.

Механика: ставка не меняет вес голоса (правила majority/minority/median
остаются честными), а участвует в фонде дня. После подсчёта фонд делится так:

- 97% — поставившим на верный путь, пропорционально ставкам;
- 2% — угадавшим верный путь без ставки, поровну (только с привязанным
  кошельком; если получателей нет — доля уходит в призовой фонд);
- 0,5% — хранителю игры;
- 0,5% — в копилку месяца: в конце месяца её забирает игрок (игроки)
  с максимумом верных ответов (/top).

Если на победивший путь не поставлено ни одной подтверждённой ставки —
все ставки возвращаются полностью, без рейка и копилки. Отклонённые
лимитами и неподтверждённые ставки возвращаются всегда.

Сети изолированы: каждая ставка и выплата помечена network (mainnet/testnet),
финализация и watcher работают только со ставками активной сети — тестнет
можно гонять вторым контуром, не трогая основной фонд.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import LeaderboardPot, Payout, Player, Round, RoundStatus, Stake, Vote
from app.ton_utils import to_nano


def current_network() -> str:
    return "testnet" if settings.is_testnet else "mainnet"


def split_pot(prize_pool_nanotons: int, entries: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Делит фонд пропорционально ставкам. Детерминированно: пыль от
    целочисленного деления достаётся крупнейшей ставке (при равенстве —
    меньшему player_id)."""
    if prize_pool_nanotons <= 0 or not entries:
        return []
    total = sum(amount for _pid, amount in entries)
    if total <= 0:
        return []
    ordered = sorted(entries, key=lambda item: (-item[1], item[0]))
    shares = [(pid, prize_pool_nanotons * amount // total) for pid, amount in ordered]
    dust = prize_pool_nanotons - sum(amount for _pid, amount in shares)
    if dust > 0:
        first_pid, first_amount = shares[0]
        shares[0] = (first_pid, first_amount + dust)
    return shares


def split_equal(total_nanotons: int, player_ids: list[int]) -> dict[int, int]:
    """Делит сумму поровну; пыль достаётся меньшему player_id."""
    ids = sorted(set(player_ids))
    if total_nanotons <= 0 or not ids:
        return {}
    base = total_nanotons // len(ids)
    shares = {pid: base for pid in ids}
    dust = total_nanotons - base * len(ids)
    if dust > 0:
        shares[ids[0]] += dust
    return shares


async def register_stake(
    session: AsyncSession,
    round_row: Round,
    player: Player,
    amount_nanotons: int,
    tx_hash: str,
    memo: str = "",
) -> str:
    """Проверяет и записывает ставку. Возвращает статус для логов/ответа."""
    if not settings.ton_enabled:
        return "disabled"
    if round_row.status != RoundStatus.OPEN:
        return "closed"
    duplicate = await session.execute(select(Stake.id).where(Stake.tx_hash == tx_hash))
    if duplicate.scalar_one_or_none() is not None:
        return "duplicate_tx"
    existing = await session.execute(
        select(Stake).where(Stake.round_id == round_row.id, Stake.player_id == player.id)
    )
    if existing.scalar_one_or_none() is not None:
        return "already_staked"
    min_nano = to_nano(settings.stake_min_ton)
    status = "pending"
    reason = ""
    if amount_nanotons < min_nano:
        status, reason = "rejected", "too_small"
    session.add(
        Stake(
            round_id=round_row.id,
            player_id=player.id,
            amount_nanotons=amount_nanotons,
            tx_hash=tx_hash,
            memo=memo[:64],
            network=current_network(),
            status=status,
        )
    )
    await session.commit()
    return reason or "ok"


async def confirm_stake(session: AsyncSession, tx_hash: str) -> bool:
    result = await session.execute(select(Stake).where(Stake.tx_hash == tx_hash))
    stake = result.scalar_one_or_none()
    if stake is None or stake.status != "pending":
        return False
    round_row = await session.get(Round, stake.round_id)
    if round_row is None or round_row.status != RoundStatus.OPEN:
        return False
    stake.status = "confirmed"
    stake.confirmed_at = datetime.now(timezone.utc)
    await session.commit()
    return True


async def finalize_day_payouts(session: AsyncSession, round_row: Round) -> int:
    """После finish_tally: создаёт выплаты призов/возвратов. Отправка — в ton_pay.

    Работает только со ставками активной сети; идемпотентность — по наличию
    выплат этой сети за раунд, поэтому mainnet и testnet финализируются
    независимо.
    """
    if round_row.status != RoundStatus.CLOSED or round_row.payouts_finalized:
        return 0
    network = current_network()
    already = await session.execute(
        select(Payout.id).where(Payout.round_id == round_row.id, Payout.network == network).limit(1)
    )
    if already.scalar_one_or_none() is not None:
        round_row.payouts_finalized = True
        await session.commit()
        return 0

    scope = [Stake.round_id == round_row.id, Stake.network == network]
    confirmed = list(
        (await session.execute(select(Stake).where(*scope, Stake.status == "confirmed"))).scalars().all()
    )
    stuck = list(
        (
            await session.execute(
                select(Stake).where(*scope, Stake.status.in_(["rejected", "pending"]))
            )
        )
        .scalars()
        .all()
    )

    async def add_payout(stake: Stake, kind: str, amount: int) -> int:
        dest = await _wallet_of(session, stake.player_id)
        session.add(
            Payout(
                round_id=round_row.id,
                player_id=stake.player_id,
                kind=kind,
                amount_nanotons=amount,
                dest_address=dest or "",
                network=network,
            )
        )
        return 1

    async def add_treasury_payout(kind: str, amount: int) -> int:
        """Доля казны без игрока: рейк хранителя или копилка месяца."""
        session.add(
            Payout(
                round_id=round_row.id,
                player_id=None,
                kind=kind,
                amount_nanotons=amount,
                dest_address=settings.owner_wallet_address or "",
                network=network,
            )
        )
        return 1

    created = 0
    pot = sum(stake.amount_nanotons for stake in confirmed)

    if not confirmed:
        round_row.pot_nanotons = 0
        round_row.rake_nanotons = 0
    else:
        winners_result = await session.execute(
            select(Vote.player_id).where(
                Vote.round_id == round_row.id,
                Vote.card_position == round_row.winner_card,
            )
        )
        winner_ids = {row[0] for row in winners_result.all()}
        staked_ids = {stake.player_id for stake in confirmed}
        winning_stakes = [stake for stake in confirmed if stake.player_id in winner_ids]

        if not winning_stakes:
            for stake in confirmed:
                created += await add_payout(stake, "refund", stake.amount_nanotons)
            round_row.pot_nanotons = pot
            round_row.rake_nanotons = 0
        else:
            owner_bp = round(settings.owner_rake_pct * 100)
            board_bp = round(settings.leaderboard_rake_pct * 100)
            free_bp = round(settings.free_winners_pct * 100)
            house_cut = pot * owner_bp // 10_000
            board_cut = pot * board_bp // 10_000
            free_pool = pot * free_bp // 10_000
            prize_pool = pot - house_cut - board_cut - free_pool

            # Угадавшие без ставки: поровну и только с привязанным кошельком —
            # без получателей доля уходит в призовой фонд.
            free_ids = sorted(winner_ids - staked_ids)
            wallets: dict[int, str] = {}
            for pid in free_ids:
                wallet = await _wallet_of(session, pid)
                if wallet:
                    wallets[pid] = wallet
            free_shares = split_equal(free_pool, list(wallets))
            if not free_shares:
                prize_pool += free_pool

            shares = split_pot(prize_pool, [(s.player_id, s.amount_nanotons) for s in winning_stakes])
            share_by_player = dict(shares)
            for stake in confirmed:
                if stake.player_id in share_by_player:
                    created += await add_payout(stake, "prize", share_by_player[stake.player_id])
                # Проигравший не получает ничего: ставка сгорает в фонд.
            for pid, amount in free_shares.items():
                session.add(
                    Payout(
                        round_id=round_row.id,
                        player_id=pid,
                        kind="bonus",
                        amount_nanotons=amount,
                        dest_address=wallets[pid],
                        network=network,
                    )
                )
                created += 1
            if house_cut > 0:
                created += await add_treasury_payout("rake", house_cut)
            if board_cut > 0:
                created += await add_treasury_payout("leaderboard", board_cut)
                month = round_row.tally_ends_at.strftime("%Y-%m")
                pot_row = (await session.execute(
                    select(LeaderboardPot).where(LeaderboardPot.month == month)
                )).scalar_one_or_none()
                if pot_row is None:
                    session.add(LeaderboardPot(month=month, nanotons=board_cut))
                else:
                    pot_row.nanotons += board_cut
            round_row.pot_nanotons = pot
            round_row.rake_nanotons = house_cut + board_cut

    for stake in stuck:
        created += await add_payout(stake, "refund", stake.amount_nanotons)

    round_row.payouts_finalized = True
    await session.commit()
    return created


async def _wallet_of(session: AsyncSession, player_id: int) -> str | None:
    player = await session.get(Player, player_id)
    return player.wallet_address if player else None
