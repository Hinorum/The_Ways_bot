"""Операционная наблюдаемость: снимок состояния для /health и алерты админу.

Всё, что раньше было видно только по логам, собирается в один JSON:
возраст последнего тика планировщика, курсор TON-watcher'а, глубина очереди
выплат, dead-letter хвост, текущий день. Аномалии (зависший watcher,
долгая очередь выплат, неустранимые failed) деликатно репортятся админу
не чаще раза в час на категорию — троттлинг живёт в watcher_state.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from aiogram import Bot
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Income, Payout, Round, RoundStatus, Stake, WatcherState

logger = logging.getLogger(__name__)

PROCESS_START = time.time()

TICK_KEY = "last_tick_iso"
ALERT_WATCHER_KEY = "alert_watcher_ts"
ALERT_QUEUE_KEY = "alert_queue_ts"
ALERT_DEAD_KEY = "alert_dead_ts"
ALERT_TICK_KEY = "alert_tick_ts"
ALERT_BALANCE_KEY = "alert_balance_ts"
ALERT_REFUND_KEY = "alert_refund_ts"
ALERT_STAKE_KEY = "alert_stake_ts"
_ALERT_COOLDOWN = timedelta(hours=1)
_WATCHER_STALE_AFTER = timedelta(minutes=30)
_QUEUE_OLD_AFTER = timedelta(minutes=30)
# Возврат ставки (refund) считается «застрявшим», когда ждёт отправки дольше
# этого окна: игрок остаётся с зависшими деньгами, хранителю нужно узнать.
_REFUND_OLD_AFTER = timedelta(minutes=30)
# Перевод, который так и не подтвердился в срок — необработанная ставка.
# Порог — двойное окно подтверждения ставки из настроек.
_STAKE_CONFIRM_STALE_MULT = 2
# Тики идут каждые 15 секунд: тишина дольше пары минут — процесс болен.
_TICK_STALE_AFTER = timedelta(minutes=5)
# Допуск сверки баланса казначея с БД: сгоревший газ исходящих переводов
# и мелкий ручной вывод не должны будить админа ложной тревогой.
_BALANCE_TOLERANCE_NANO = 50_000_000  # 0.05 Gram

# Пауза игры (стоп-кран): метка времени включения и причина. Живут в
# watcher_state — переживают рестарт, видны всем процессам и джобам.
PAUSE_KEY = "game_paused_iso"
PAUSE_REASON_KEY = "game_paused_reason"
# Виды корректировок казны: ручной вывод хранителя / ручное пополнение.
# Пишутся в Income (unit_ref «manual:<uuid>»), попадают в формулу сверки.
MANUAL_OUT_KIND = "manual_out"
MANUAL_IN_KIND = "manual_in"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (_now() - moment).total_seconds())


async def _get_state(session, key: str) -> str | None:
    row = await session.get(WatcherState, key)
    return row.value if row is not None else None


async def _set_state(session, key: str, value: str) -> None:
    row = await session.get(WatcherState, key)
    if row is None:
        session.add(WatcherState(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def mark_tick() -> None:
    """Планировщик отмечается каждый тик: /health видит зависший процесс."""
    async with SessionLocal() as session:
        await _set_state(session, TICK_KEY, _now().isoformat())


async def snapshot() -> dict:
    """Состояние игры одним словарём для /health (мониторинг Render/UptimeRobot)."""
    async with SessionLocal() as session:
        latest = (
            await session.execute(select(Round).order_by(Round.day_index.desc()).limit(1))
        ).scalar_one_or_none()
        queue_count = (
            await session.execute(
                select(func.count())
                .select_from(Payout)
                .where(Payout.status.in_(["pending", "sending"]))
            )
        ).scalar_one()
        oldest_pending = (
            await session.execute(
                select(func.min(Payout.created_at)).where(
                    Payout.status.in_(["pending", "sending"])
                )
            )
        ).scalar_one()
        dead_count = (
            await session.execute(
                select(func.count()).select_from(Payout).where(Payout.status == "failed")
            )
        ).scalar_one()
        # Разбивка очереди по типу выплаты: призы игрокам против возвратов ставок
        # и долей казны. Именно здесь видно, сколько игроков не получили ставку
        # обратно, не ныряя в /payouts.
        pending_by_kind = {
            kind: count
            for kind, count in (
                await session.execute(
                    select(Payout.kind, func.count())
                    .where(Payout.status.in_(["pending", "sending"]))
                    .group_by(Payout.kind)
                )
            ).all()
        }
        dead_by_kind = {
            kind: count
            for kind, count in (
                await session.execute(
                    select(Payout.kind, func.count())
                    .where(Payout.status == "failed")
                    .group_by(Payout.kind)
                )
            ).all()
        }
        # Необработанные переводы: ставки, что увидели в цепочке, но ещё не
        # подтвердили по возрасту (или зависли). Прямой ответ на «сколько
        # переводов висит в необработанных».
        pending_stakes = (
            await session.execute(
                select(func.count()).select_from(Stake).where(Stake.status == "pending")
            )
        ).scalar_one()
        cursor_iso = await _get_state(session, "ton_watch_beat_iso")
        payload = {
            "status": "ok",
            "uptime_seconds": round(time.time() - PROCESS_START, 1),
            "last_tick_age": _age_seconds(await _get_state(session, TICK_KEY)),
            "round": None,
            "payout_queue": int(queue_count),
            "payout_pending_by_kind": pending_by_kind,
            "payout_dead_by_kind": dead_by_kind,
            "pending_stakes": int(pending_stakes),
            "oldest_payout_age": None,
            "dead_letter_payouts": int(dead_count),
            "watcher_beat_age": None,
            "watcher_source": None,
            # Диагностика окружения: видно, что реально дошло до процесса.
            "ton_enabled": bool(settings.ton_enabled),
            "ton_network": "testnet" if settings.is_testnet else "mainnet",
        }
        if oldest_pending is not None:
            moment = oldest_pending if oldest_pending.tzinfo else oldest_pending.replace(tzinfo=timezone.utc)
            payload["oldest_payout_age"] = max(0.0, (_now() - moment).total_seconds())
        if settings.ton_enabled:
            payload["watcher_beat_age"] = _age_seconds(cursor_iso)
            # Локальный импорт: ton_watch тянет ставки/выплаты, ops должен
            # оставаться лёгким для /health при любых состояниях модулей.
            from app.ton_watch import SOURCE_KEY

            payload["watcher_source"] = await _get_state(session, SOURCE_KEY)
        if latest is not None:
            payload["round"] = {
                "day_index": latest.day_index,
                "status": latest.status.value if isinstance(latest.status, RoundStatus) else str(latest.status),
                "voting_ends_at": latest.voting_ends_at.isoformat(),
            }
        return payload


async def notify_admins(bot: Bot | None, text: str) -> None:
    """Разослать служебное сообщение всем хранителям; ошибки не мешают тику."""
    if bot is None or not settings.admin_id_set:
        return
    for admin_id in settings.admin_id_set:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("Служебный алерт админу %s не доставлен: %s", admin_id, exc)


async def _throttled(session, key: str) -> bool:
    """True, если по этой категории пора кричать (кулдаун раз в час)."""
    raw = await _get_state(session, key)
    if raw:
        try:
            if _now() - datetime.fromisoformat(raw) < _ALERT_COOLDOWN:
                return False
        except ValueError:
            pass
    await _set_state(session, key, _now().isoformat())
    return True


async def check_anomalies(bot: Bot | None) -> list[str]:
    """Фоновые проверки раз в минутный цикл обслуживания; возвращает список проблем."""
    problems: list[str] = []
    async with SessionLocal() as session:
        # 0. Планировщик молчит — дни не закрываются, выплаты не уходят.
        tick_age = _age_seconds(await _get_state(session, TICK_KEY))
        if tick_age is not None and tick_age > _TICK_STALE_AFTER.total_seconds():
            note = f"планировщик не тикает {int(tick_age // 60)} мин"
            problems.append(note)
            if await _throttled(session, ALERT_TICK_KEY):
                await notify_admins(
                    bot,
                    f"⚠️ {note.capitalize()}. Дни не переключаются, смотрите логи сервиса.",
                )
        # 1. Watcher не завершает успешные циклы — ставки перестают находиться.
        # Сердцебиение ставит каждый цикл с живым TonAPI, даже если переводов
        # нет: тишина в цепочке — здоровье, а не простой. Курсор для этого
        # не годится: он двигается только переводами.
        if settings.ton_enabled:
            from app.ton_watch import BEAT_KEY

            beat_age = _age_seconds(await _get_state(session, BEAT_KEY))
            watcher_note: str | None = None
            if beat_age is None:
                watcher_note = "watcher ещё ни разу не завершал цикл"
            elif beat_age > _WATCHER_STALE_AFTER.total_seconds():
                watcher_note = f"watcher молчит {int(beat_age // 60)} мин"
            if watcher_note is not None:
                problems.append(watcher_note)
                if await _throttled(session, ALERT_WATCHER_KEY):
                    await notify_admins(
                        bot,
                        f"⚠️ TON-watcher отстаёт: {watcher_note}. Ставки копятся необработанными.",
                    )
        # 2. Очередь выплат старше получаса — казначей застрял или сеть лежит.
        oldest = (
            await session.execute(
                select(func.min(Payout.created_at)).where(
                    Payout.status.in_(["pending", "sending"]),
                    Payout.amount_nanotons > 0,
                )
            )
        ).scalar_one()
        if oldest is not None:
            moment = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
            age_min = int((_now() - moment).total_seconds() // 60)
            if age_min >= _QUEUE_OLD_AFTER.total_seconds() // 60:
                problems.append(f"очередь выплат стоит {age_min} мин")
                reason = ""
                if not settings.ton_enabled:
                    reason = " Причина: TON выключен (TON_ENABLED=false), ретраи не идут."
                elif not settings.active_treasury_mnemonic:
                    reason = " Причина: нет мнемоники казначея для активной сети (TREASURY_TESTNET_MNEMONIC?)."
                if await _throttled(session, ALERT_QUEUE_KEY):
                    await notify_admins(
                        bot,
                        f"⚠️ Очередь выплат не двигается {age_min} мин.{reason} Проверь казначея/сеть.",
                    )
        # 3. Dead-letter выплаты существуют — деньги ждут ручного разбора.
        dead = (
            await session.execute(
                select(Payout.id, Payout.last_error).where(Payout.status == "failed").limit(20)
            )
        ).all()
        if dead:
            problems.append(f"dead-letter выплат: {len(dead)}")
            reasons = [f"#{row_id} {reason}" for row_id, reason in dead[:2] if reason]
            reason_note = f": {'; '.join(reasons)}" if reasons else ""
            if await _throttled(session, ALERT_DEAD_KEY):
                await notify_admins(
                    bot,
                    "⚠️ Есть безнадёжные выплаты (failed после всех ретраев): "
                    f"{', '.join(str(row_id) for row_id, _r in dead[:10])}{reason_note}. "
                    "Разбор: /payouts → /payout <id> retry|spam перед сбросом игры.",
                )
        # 3b. Возврат ставки (refund) висит неотправленным: игрок не получил
        #     деньги обратно. Отдельный целевой алерт, а не общий «очередь стоит».
        oldest_refund = (
            await session.execute(
                select(func.min(Payout.created_at)).where(
                    Payout.kind == "refund",
                    Payout.status.in_(["pending", "sending"]),
                    Payout.amount_nanotons > 0,
                )
            )
        ).scalar_one()
        refund_dead = (
            await session.execute(
                select(func.count())
                .select_from(Payout)
                .where(Payout.kind == "refund", Payout.status == "failed")
            )
        ).scalar_one()
        if oldest_refund is not None:
            moment = (
                oldest_refund
                if oldest_refund.tzinfo
                else oldest_refund.replace(tzinfo=timezone.utc)
            )
            age_min = int((_now() - moment).total_seconds() // 60)
            if age_min >= _REFUND_OLD_AFTER.total_seconds() // 60:
                problems.append(f"возврат ставки ждёт {age_min} мин")
                tail = ""
                if refund_dead:
                    tail = f" Плюс {refund_dead} безнадёжных возврата (failed)."
                if await _throttled(session, ALERT_REFUND_KEY):
                    await notify_admins(
                        bot,
                        "⚠️ Возврат ставки не доставлен: игрок не получил деньги "
                        f"обратно уже {age_min} мин.{tail} Разбор: /payouts",
                    )
        # 3c. Необработанные переводы-ставки висят дольше двойного окна
        #     подтверждения — ставки копятся, никто не получает ни статус, ни
        #     возврат. Прямой ответ на «сколько переводов не обработано».
        if settings.ton_enabled:
            stale_threshold = settings.stake_confirm_seconds * _STAKE_CONFIRM_STALE_MULT
            oldest_stake = (
                await session.execute(
                    select(func.min(Stake.created_at)).where(Stake.status == "pending")
                )
            ).scalar_one()
            if oldest_stake is not None:
                moment = (
                    oldest_stake
                    if oldest_stake.tzinfo
                    else oldest_stake.replace(tzinfo=timezone.utc)
                )
                if (_now() - moment).total_seconds() >= stale_threshold:
                    pending_stakes_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(Stake)
                            .where(Stake.status == "pending")
                        )
                    ).scalar_one()
                    problems.append(f"ставок не обработано: {pending_stakes_count}")
                    if await _throttled(session, ALERT_STAKE_KEY):
                        await notify_admins(
                            bot,
                            "⚠️ Переводы-ставки не обрабатываются: "
                            f"{pending_stakes_count} висят без подтверждения дольше "
                            "положенного. /stakes",
                        )
        # 4. Сверка баланса казначея с учётом БД. Две беды разного рода:
        #    дефицит под очередь (пополни — и всё уйдёт само) и расхождение
        #    с ожиданиями (ручной вывод, потерянные средства, чужой доступ).
        if settings.ton_enabled and settings.active_treasury_address:
            balance_note = await _treasury_balance_anomaly(session)
            if balance_note is not None:
                problems.append(balance_note)
                if await _throttled(session, ALERT_BALANCE_KEY):
                    await notify_admins(
                        bot,
                        f"⚠️ Казначей: {balance_note}. Детали: /treasury и /payouts.\n"
                        "Это был твой ручной перевод — закрой расхождение: /adjust",
                    )
    return problems


class TreasuryDrift(NamedTuple):
    """Снимок сверки «баланс цепочки ↔ ожидания БД» одним объектом."""

    balance_nanotons: int
    expected_nanotons: int
    unpaid_nanotons: int
    tolerance_nanotons: int

    @property
    def drift_nanotons(self) -> int:
        """Положительный — на цепи МЕНЬШЕ ожиданий (вывод/пропажа),
        отрицательный — больше (незаметное пополнение)."""
        return self.expected_nanotons - self.balance_nanotons

    @property
    def beyond_tolerance(self) -> bool:
        return abs(self.drift_nanotons) > self.tolerance_nanotons


async def treasury_expected_state(session) -> TreasuryDrift | None:
    """Баланс цепочки против ожиданий БД; None — баланс недоступен.

    Ожидаемый остаток = все входящие переводы казны (ставки и revote-оплата,
    это строки Income kind="ton") + ручные пополнения − ручные выводы − все
    выплаты (sent уже ушли, pending/sending ещё уйдут). Допуск покрывает
    сгоревший газ исходящих переводов.

    ВАЖНО: подтверждённые ставки отдельно НЕ суммируем — каждый входящий
    перевод уже создаёт строку Income kind="ton" (ton_watch._ledger_incoming /
    _process_revote), и ставка дважды посчиталась бы (двойной учёт 1 Gram —
    неправдоподобные «пропажи» на ровном месте).
    """
    from app.ton_pay import fetch_account_state

    try:
        balance, _status, _source = await fetch_account_state()
    except Exception as exc:
        logger.warning("Баланс казначея для сверки не прочитан: %s", exc)
        return None
    if balance is None:
        return None
    network = "testnet" if settings.is_testnet else "mainnet"
    unpaid = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Payout.amount_nanotons), 0)).where(
                    Payout.status.in_(["pending", "sending"]),
                    Payout.network == network,
                )
            )
        ).scalar_one()
    )
    sent = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Payout.amount_nanotons), 0)).where(
                    Payout.status == "sent", Payout.network == network,
                )
            )
        ).scalar_one()
    )
    sent_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Payout).where(
                    Payout.status == "sent", Payout.network == network,
                )
            )
        ).scalar_one()
    )
    revotes = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Income.amount_nanotons), 0)).where(
                    Income.kind == "ton",
                    Income.network == network,
                )
            )
        ).scalar_one()
    )
    manual_in = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Income.amount_nanotons), 0)).where(
                    Income.kind == MANUAL_IN_KIND,
                )
            )
        ).scalar_one()
    )
    manual_out = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Income.amount_nanotons), 0)).where(
                    Income.kind == MANUAL_OUT_KIND,
                )
            )
        ).scalar_one()
    )
    expected = revotes + manual_in - manual_out - sent - unpaid
    # Газ сгорает на каждом исходящем переводе; допуск = база + запас по числу.
    from app.ton_utils import to_nano

    tolerance = _BALANCE_TOLERANCE_NANO + sent_count * 2 * to_nano(settings.payout_fee_gram)
    return TreasuryDrift(
        balance_nanotons=balance,
        expected_nanotons=expected,
        unpaid_nanotons=unpaid,
        tolerance_nanotons=tolerance,
    )


async def _treasury_balance_anomaly(session) -> str | None:
    """Сравнивает баланс цепочки с ожиданиями БД. None — всё сходится.

    Две беды разного рода: дефицит под очередь выплат (пополни казначея)
    и расхождение с ожиданиями (ручной вывод или пропажа — закрывается
    командой /adjust). TON-строки Income размечены сетью (network), поэтому
    сверка идёт по активному контуру mainnet/testnet без смешения меток.
    """
    state = await treasury_expected_state(session)
    if state is None:
        return None
    if state.balance_nanotons < state.unpaid_nanotons:
        return (
            f"баланса {state.balance_nanotons / 1e9:.4f} Gram не хватит на очередь выплат "
            f"({state.unpaid_nanotons / 1e9:.4f} Gram) — пополни казначея"
        )
    if state.balance_nanotons + state.tolerance_nanotons < state.expected_nanotons:
        return (
            f"баланс {state.balance_nanotons / 1e9:.4f} Gram ниже ожиданий БД "
            f"(~{state.expected_nanotons / 1e9:.4f} Gram): ручной вывод или пропажа средств? "
            f"Закрыть расхождение: /adjust"
        )
    return None


async def record_manual_adjustment(
    session,
    kind: str,
    amount_nanotons: int,
    note: str = "",
) -> Income:
    """Корректировка казны: «это был мой ручной вывод» либо «пропажа/пополнение».

    Пишет строку в Income-леджер (unit_ref «manual:<uuid>» — уникальность
    бесплатно), после чего формула ожиданий в treasury_expected_state
    сходится с реальностью и часовой алерт замолкает сам.
    """
    if kind not in {MANUAL_OUT_KIND, MANUAL_IN_KIND}:
        raise ValueError(f"Неизвестный вид корректировки: {kind}")
    amount = int(amount_nanotons)
    if amount <= 0:
        raise ValueError("Сумма корректировки должна быть положительной")
    row = Income(
        kind=kind,
        amount_nanotons=amount,
        player_id=None,
        round_id=None,
        unit_ref=f"manual:{uuid.uuid4().hex}",
        note=(note or "")[:200],
    )
    session.add(row)
    await session.commit()
    logger.info(
        "Корректировка казны: %s %.4f Gram (%s)", kind, amount / 1e9, note or "без комментария"
    )
    return row


# ---------- Пауза игры (стоп-кран хранителя) ----------


async def is_game_paused(session) -> bool:
    """True, пока игра остановлена командой /pause или кнопкой пропажи."""
    return bool(await _get_state(session, PAUSE_KEY))


async def paused_reason(session) -> str | None:
    """Причина паузы (для пульта и сообщений игрокам) или None."""
    reason = await _get_state(session, PAUSE_REASON_KEY)
    return reason or None


async def set_game_paused(session, paused: bool, reason: str = "") -> bool:
    """Включает/снимает паузу. Возвращает True, если состояние изменилось.

    Повторная установка того же состояния — no-op: двойное нажатие кнопки
    не рассылает игрокам второе уведомление и не затирает причину.
    """
    if paused == await is_game_paused(session):
        return False
    if paused:
        await _set_state(session, PAUSE_KEY, _now().isoformat())
        if reason:
            await _set_state(session, PAUSE_REASON_KEY, reason[:200])
    else:
        await _set_state(session, PAUSE_KEY, "")
        await _set_state(session, PAUSE_REASON_KEY, "")
    logger.info("Пауза игры: %s (%s)", "включена" if paused else "снята", reason or "—")
    return True
