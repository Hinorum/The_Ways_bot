"""Ставки TON на путь: приём, фонд дня и распределение выплат.

Механика: ставка не меняет вес голоса (правила majority/minority/median
остаются честными), а участвует в фонде дня. После подсчёта фонд делится так:

- 96% — поставившим на верный путь, пропорционально ставкам. Из этой части
  ЗАРАНЕЕ вычитается газ сети (~payout_fee_gram на каждый перевод), поэтому
  победитель получает приз «чистыми», а казначей не платит за чужие транзы;
- 1% — Фонд Стаи: накопительный, разыгрывается вручную хранителем (см. /panel);
- 2% — копилка недели: каждый день капает сюда, в понедельник сумму делят
  топ-3 недели по числу верных ответов (см. app/leaderboard.py);
- 0,5% — хранителю игры;
- 0,5% — в копилку месяца: в конце месяца её забирает игрок (игроки)
  с максимумом верных ответов (/top).

Доли меньше min_payout_gram не превращаются в дохлые переводы: они капают
в копилку недели и видны в строке недели поста итогов. Если комиссии съели
призовой пул целиком (экзотика: много микоставок), весь пул уходит туда же.

Если на победивший путь не поставлено ни одной подтвержденной ставки —
все ставки возвращаются без рейка и копилок, но с вычетом газа сети (~payout_fee_gram
с каждого перевода, как и у призов: казначей не финансирует чужие транзы).
Отклонённые лимитами и неподтверждённые ставки возвращаются всегда (так же с
вычетом газа); если газ больше самой ставки — возвращать нечего.

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
from app.models import (
    LeaderboardPot,
    PackFund,
    PackFundLedger,
    Payout,
    Player,
    Round,
    RoundStatus,
    Stake,
    Vote,
    WeeklyPot,
)
from app.ton_utils import from_nano, to_nano
from app.weeks import iso_week_key


logger = logging.getLogger(__name__)


def current_network() -> str:
    return "testnet" if settings.is_testnet else "mainnet"


def refund_net_amount(stake_amount_nanotons: int) -> int:
    """Сумма возврата ИЗ СТАВКИ: с возвратов тоже удерживается газ сети.

    По умолчанию с каждого возврата держится плоская комиссия payout_fee_gram
    (казначей не финансирует чужие переводы из своего остатка). Если настроен
    refund_fee_ratio > 0 — комиссия становится пропорциональной сумме ставки
    (возврат = ставка × (1 − доля)), чтобы мелкие возвраты не теряли
    непропорционально много. Если газ больше самой ставки (экзотика:
    микро-отклонённые ставки), возвращать нечего — возвращаем 0, чтобы не
    плодить дохлый/отрицательный перевод.
    """
    ratio = getattr(settings, "refund_fee_ratio", 0.0) or 0.0
    if ratio > 0:
        return max(0, int(stake_amount_nanotons * (1.0 - ratio)))
    return max(0, stake_amount_nanotons - to_nano(settings.payout_fee_gram))


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
    # Серверный гейт «бесплатного дня». UI-гейты закрывают кнопки, но и прямой
    # вход денежного контура (наблюдатель блокчейна, revote, авто-грант по
    # сумме) не должен принимать деньги за день, который живёт без ставок.
    if not round_row.money_mode:
        return "money_off"
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
                refund = refund_net_amount(stake.amount_nanotons)
                if refund > 0:
                    created += add_payout(stake, "refund", refund)
            round_row.pot_nanotons = pot
            round_row.rake_nanotons = 0
        else:
            owner_bp = round(settings.owner_rake_pct * 100)
            board_bp = round(settings.leaderboard_rake_pct * 100)
            weekly_bp = round(settings.weekly_pot_pct * 100)
            fund_bp = round(settings.pack_fund_pct * 100)
            house_cut = pot * owner_bp // 10_000
            board_cut = pot * board_bp // 10_000
            weekly_cut = pot * weekly_bp // 10_000
            fund_cut = pot * fund_bp // 10_000
            prize_pool = pot - house_cut - board_cut - weekly_cut - fund_cut

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
            # Фонд Стаи: неубывающее накопление без периода раздачи. Единственная
            # строка-накопитель; деньги остаются на кошельке казначея и забираются
            # хранителем вручную (см. /panel, ручной вывод).
            if fund_cut > 0:
                fund_row = (
                    await session.execute(
                        select(PackFund).order_by(PackFund.id).limit(1)
                    )
                ).scalar_one_or_none()
                if fund_row is None:
                    session.add(PackFund(nanotons=fund_cut))
                else:
                    fund_row.nanotons += fund_cut
                # Аудит: каждое начисление пишется в прозрачный журнал фонда.
                session.add(
                    PackFundLedger(
                        entry_type="in",
                        amount_nanotons=fund_cut,
                        round_id=round_row.id,
                        note=f"1% банка дня {round_row.day_index}",
                    )
                )
            round_row.pot_nanotons = pot
            round_row.rake_nanotons = house_cut + board_cut

    for stake in stuck:
        refund = refund_net_amount(stake.amount_nanotons)
        if refund > 0:
            created += add_payout(stake, "refund", refund)

    await session.commit()
    return created


async def refundable_stakes(session, limit: int = 25) -> list[tuple[Stake, Player | None, Round | None]]:
    """Ставки, которые можно вернуть вручную из /panel: ещё не «засчитанные»
    (pending/rejected) и не имеющие незакрытого refund-выплата. Возвращает
    пары (ставка, игрок, раунд) для списка хранителю."""
    subq = (
        select(Payout.round_id, Payout.player_id)
        .where(
            Payout.kind == "refund",
            Payout.status.notin_(["sent", "dismissed"]),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(Stake, Player, Round)
            .join(Player, Player.id == Stake.player_id)
            .join(Round, Round.id == Stake.round_id)
            .outerjoin(
                subq,
                (subq.c.round_id == Stake.round_id) & (subq.c.player_id == Stake.player_id),
            )
            .where(
                Stake.status.in_(["pending", "rejected"]),
                Stake.network == current_network(),
                subq.c.player_id.is_(None),
            )
            .order_by(Stake.id.desc())
            .limit(limit)
        )
    ).all()
    return rows


async def create_manual_refund(session, stake_id: int) -> str:
    """Ручной возврат ставки хранителем (см. /panel → «Возвраты»).

    Только для ещё не «засчитанных» ставок status in (pending, rejected) —
    подтверждённые (confirmed) разбираются автоматически при финализации дня,
    и ручной возврат там создал бы двойную выплату. Идемпотентно: если для
    (round_id, player_id) уже есть незакрытый refund-выплат — не дублируем.
    Деньги уходят обычной очередью выплат (dispatch_pending_payouts).
    """
    stake = await session.get(Stake, stake_id)
    if stake is None:
        return "нет такой ставки"
    if stake.network != current_network():
        return "ставка из другого контура сети"
    if stake.status == "refunded":
        return "возврат этой ставки уже оформлен ранее"
    if stake.status not in ("pending", "rejected"):
        return (
            "ставка уже засчитана (confirmed) — она разберётся при итогах дня сама. "
            "Ручной возврат не нужен."
        )
    dup = (
        await session.execute(
            select(Payout).where(
                Payout.kind == "refund",
                Payout.round_id == stake.round_id,
                Payout.player_id == stake.player_id,
                Payout.status.notin_(["sent", "dismissed"]),
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        return f"возврат уже создан (выплата #{dup.id})"
    player = await session.get(Player, stake.player_id)
    wallet = player.wallet_address if player is not None else ""
    if not wallet:
        return "у игрока не привязан кошелёк — возврат невозможен"
    refund = refund_net_amount(stake.amount_nanotons)
    if refund <= 0:
        return "сумма ставки не покрывает газ сети — возвращать нечего"
    session.add(
        Payout(
            round_id=stake.round_id,
            player_id=stake.player_id,
            kind="refund",
            amount_nanotons=refund,
            dest_address=wallet,
            network=current_network(),
        )
    )
    stake.status = "refunded"
    await session.commit()
    return f"возврат {from_nano(refund):.4g} Gram поставлен в очередь (выплата создана)"


async def record_fund_dispense(
    session: AsyncSession, amount_nanotons: int, note: str = ""
) -> str:
    """Прозрачная ручная раздача Фонда Стаи: списывает баланс и пишет «out» в журнал.

    Сам физический перевод делает обычная очередь выплат с казначея; здесь —
    аудит-след, чтобы баланс фонда и его журнал оставались честными (сумма
    поступлений минус документированные раздачи). Возвращает сообщение для
    хранителя или строку-ошибку без изменения данных.
    """
    fund_row = (
        await session.execute(select(PackFund).order_by(PackFund.id).limit(1))
    ).scalar_one_or_none()
    balance = fund_row.nanotons if fund_row is not None else 0
    if amount_nanotons <= 0:
        return "сумма должна быть положительной"
    if amount_nanotons > balance:
        return (
            f"в фонде меньше ({from_nano(balance):.4g} Gram), чем списывается "
            f"({from_nano(amount_nanotons):.4g} Gram) — запись отменена"
        )
    if fund_row is not None:
        fund_row.nanotons = balance - amount_nanotons
    session.add(
        PackFundLedger(
            entry_type="out",
            amount_nanotons=amount_nanotons,
            round_id=None,
            note=note[:180],
        )
    )
    await session.commit()
    return f"раздача {from_nano(amount_nanotons):.4g} Gram записана во {note or 'Фонд Стаи'}"
