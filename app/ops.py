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
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Payout, Round, RoundStatus, WatcherState

logger = logging.getLogger(__name__)

PROCESS_START = time.time()

TICK_KEY = "last_tick_iso"
ALERT_WATCHER_KEY = "alert_watcher_ts"
ALERT_QUEUE_KEY = "alert_queue_ts"
ALERT_DEAD_KEY = "alert_dead_ts"
ALERT_TICK_KEY = "alert_tick_ts"
_ALERT_COOLDOWN = timedelta(hours=1)
_WATCHER_STALE_AFTER = timedelta(minutes=30)
_QUEUE_OLD_AFTER = timedelta(minutes=30)
# Тики идут каждые 15 секунд: тишина дольше пары минут — процесс болен.
_TICK_STALE_AFTER = timedelta(minutes=5)


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
        cursor_iso = await _get_state(session, "ton_watch_beat_iso")
        payload = {
            "status": "ok",
            "uptime_seconds": round(time.time() - PROCESS_START, 1),
            "last_tick_age": _age_seconds(await _get_state(session, TICK_KEY)),
            "round": None,
            "payout_queue": int(queue_count),
            "oldest_payout_age": None,
            "dead_letter_payouts": int(dead_count),
            "watcher_beat_age": None,
            "watcher_source": None,
            # Диагностика окружения: видно, что реально дошло до процесса.
            "ton_enabled": bool(settings.ton_enabled),
            "ton_network": "testnet" if settings.is_testnet else "mainnet",
            "treasury_address_set": bool(settings.active_treasury_address),
            "treasury_mnemonic_set": bool(settings.active_treasury_mnemonic),
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
    return problems
