"""Ставки TON на путь: приём, фонд дня и распределение выплат.

Механика: ставка не меняет вес голоса (правила majority/minority/median
остаются честными), а участвует в фонде дня. После подсчёта фонд делится так:

- 97% — поставившим на верный путь, пропорционально ставкам. Из этой части
  ЗАРАНЕЕ вычитается газ сети (~payout_fee_gram на каждый перевод), поэтому
  победитель получает приз «чистыми», а казначей не платит за чужие транзы;
- 2% — копилка недели: каждый день капает сюда, в понедельник сумму делят
  топ-3 недели по числу верных ответов (см. app/leaderboard.py);
- 0,5% — хранителю игры;
- 0,5% — в копилку месяца: в конце месяца её забирает игрок (игроки)
  с максимумом верных ответов (/top).

Доли меньше min_payout_gram не превращаются в дохлые переводы: они капают
в копилку недели и видны в строке недели поста итогов. Если комиссии съели
призовой пул целиком (экзотика: много микоставок), весь пул уходит туда же.

Если на победивший путь не поставлено ни одной подтвержденной ставки —
все ставки возвращаются полностью, без рейка, копилок и вычета газа.
Отклонённые лимитами и неподтверждённые ставки возвращаются всегда.

Сети изолированы: каждая ставка и выплата помечена network (mainnet/testnet),
финализация и watcher работают только со ставками активной сети — тестнет
можно гонять вторым контуром, не трогая основной фонд.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import LeaderboardPot, Payout, Player, Round, RoundStatus, Stake, Vote, WeeklyPot
from app.ton_utils import to_nano
from app.weeks import iso_week_key


logger = logging.getLogger(__name__)


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
    # Стоп-кран игры: ставка не создаётся, даже если сюда пришли напрямую,
    # минуя watcher (который при паузе и так возвращает переводы).
    from app.ops import is_game_paused

    if await is_game_paused(session):
        return "paused"
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

    Атомарный claim: UPDATE ... WHERE payouts_finalized = false гарантирует,
    что при параллельном вызове (tick + ton-settle) только один поток пройдёт
    дальше. Идемпотентность — по флагу round.payouts_finalized.
    """
    if round_row.status != RoundStatus.CLOSED:
        return 0
    network = current_network()
    claim = await session.execute(
        update(Round)
        .where(Round.id == round_row.id, Round.payouts_finalized.is_(False))
        .values(payouts_finalized=True)
    )
    if claim.rowcount == 0:
        return 0
    await session.commit()

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

    # Pre-load кошельков всех игроков одним запросом (eliminate N+1).
    all_player_ids = {s.player_id for s in confirmed + stuck}
    wallet_map: dict[int, str] = {}
    if all_player_ids:
        players_result = await session.execute(
            select(Player.id, Player.wallet_address).where(Player.id.in_(all_player_ids))
        )
        wallet_map = {pid: addr for pid, addr in players_result.all()}

    def add_payout(stake: Stake, kind: str, amount: int) -> int:
        session.add(
            Payout(
                round_id=round_row.id,
                player_id=stake.player_id,
                kind=kind,
                amount_nanotons=amount,
                dest_address=wallet_map.get(stake.player_id) or "",
                network=network,
            )
        )
        return 1

    def add_treasury_payout(kind: str, amount: int) -> int:
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
        winning_stakes = [stake for stake in confirmed if stake.player_id in winner_ids]

        if not winning_stakes:
            for stake in confirmed:
                created += add_payout(stake, "refund", stake.amount_nanotons)
            round_row.pot_nanotons = pot
            round_row.rake_nanotons = 0
        else:
            owner_bp = round(settings.owner_rake_pct * 100)
            board_bp = round(settings.leaderboard_rake_pct * 100)
            weekly_bp = round(settings.weekly_pot_pct * 100)
            house_cut = pot * owner_bp // 10_000
            board_cut = pot * board_bp // 10_000
            weekly_cut = pot * weekly_bp // 10_000
            prize_pool = pot - house_cut - board_cut - weekly_cut

            fee_nano = to_nano(settings.payout_fee_gram)
            min_payout_nano = to_nano(settings.min_payout_gram)
            net_pool = prize_pool - fee_nano * len(winning_stakes)

            dust_to_week = 0
            if net_pool < min_payout_nano:
                dust_to_week = prize_pool
                logger.warning(
                    "День %s: газ сети съел призовой пул (%d нанотонов на %d переводов) — "
                    "пул ушёл в копилку недели",
                    round_row.day_index, prize_pool, len(winning_stakes),
                )
            else:
                shares = split_pot(net_pool, [(s.player_id, s.amount_nanotons) for s in winning_stakes])
                share_by_player = dict(shares)
                for stake in confirmed:
                    share = share_by_player.get(stake.player_id)
                    if share is None:
                        continue
                    if share <= 0:
                        logger.warning(
                            "Ставка игрока %s дала нулевую долю (%d из %d) — перевод пропущен",
                            stake.player_id, share, prize_pool,
                        )
                        continue
                    if share < min_payout_nano:
                        dust_to_week += share
                        continue
                    created += add_payout(stake, "prize", share)
            if house_cut > 0:
                created += add_treasury_payout("rake", house_cut)
            if board_cut > 0:
                created += add_treasury_payout("leaderboard", board_cut)
                month = round_row.tally_ends_at.strftime("%Y-%m")
                pot_row = (await session.execute(
                    select(LeaderboardPot).where(LeaderboardPot.month == month)
                )).scalar_one_or_none()
                if pot_row is None:
                    session.add(LeaderboardPot(month=month, nanotons=board_cut))
                else:
                    pot_row.nanotons += board_cut
            week_total_cut = weekly_cut + dust_to_week
            if week_total_cut > 0:
                week = iso_week_key(round_row.opens_at)
                week_row = (await session.execute(
                    select(WeeklyPot).where(WeeklyPot.week == week)
                )).scalar_one_or_none()
                if week_row is None:
                    session.add(WeeklyPot(week=week, nanotons=week_total_cut))
                else:
                    week_row.nanotons += week_total_cut
                round_row.weekly_nanotons = week_total_cut
            round_row.pot_nanotons = pot
            round_row.rake_nanotons = house_cut + board_cut

    for stake in stuck:
        created += add_payout(stake, "refund", stake.amount_nanotons)

    await session.commit()
    return created
