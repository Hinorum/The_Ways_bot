"""Личные приглашения в стаю.

У каждого игрока есть личная ссылка ?start=ref_<id>_<токен>. Токен — HMAC
от id игрока на settings.referral_secret, поэтому вписать в ссылку чужой id
нельзя без секрета. Первый валидный переход фиксируется в таблице Referral
строго один раз на игрока; повторные /start с чужими ссылками игнорируются.
Самоподписка заблокирована (referrer_id == referred_id) — позвать самого себя
по своей же ссылке невозможно.

Пригласивший получает награду referral_pct (по умолчанию 1%) с каждой
подтверждённой ставки приведённого игрока: она копится в ReferralPot и
уходит обычной очередью выплат при достижении referral_min_payout_gram
(см. app/stakes.py — финализация дня). Размен между СВОИМИ аккаунтами
(альт приводит второго альфа реальных TON) технически не блокирован: ценой
будет 1% от реально внесённых приведённым денег, так что ферма ловит свой
же процент с живого вклада — механика сама себя обесценивает.
"""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.models import Player, Referral, ReferralPot

_BOT_USERNAME_CACHE: str | None = None


def _referral_token(player_id: int) -> str | None:
    """HMAC-подпись id игрока. None — рефералки выключены (нет секрета)."""
    if not settings.referral_secret:
        return None
    digest = hmac.new(
        settings.referral_secret.encode("utf-8"),
        str(player_id).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:10]


def referral_link(player_id: int, username: str | None) -> str | None:
    """Персональная ссылка приглашения. None — каркас выключен или нет username."""
    token = _referral_token(player_id)
    if not token or not username:
        return None
    clean = username.strip().lstrip("@")
    if not clean:
        return None
    return f"https://t.me/{clean}?start=ref_{player_id}_{token}"


def parse_referral_arg(arg: str | None) -> int | None:
    """Referrer_id из аргумента /start (ref_<id>_<токен>) при совпавшей
    подписи. Мусор, чужие id и выключенный каркас → None."""
    if not arg or not arg.startswith("ref_"):
        return None
    parts = arg.split("_", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    referrer_id = int(parts[1])
    expected = _referral_token(referrer_id)
    if expected and hmac.compare_digest(parts[2], expected):
        return referrer_id
    return None


async def record_referral(session, referrer_id: int, referred_id: int) -> bool:
    """Фиксирует приведение, если оно первое и честное. False — самоссылка,
    каркас выключен, приглашающего нет в базе или переход уже записан."""
    if referrer_id <= 0 or referred_id <= 0 or referrer_id == referred_id:
        return False
    if settings.referral_secret == "":
        return False
    if await session.get(Player, referrer_id) is None:
        return False
    already = await session.scalar(
        select(Referral.referred_id).where(Referral.referred_id == referred_id)
    )
    if already is not None:
        return False
    session.add(Referral(referrer_id=referrer_id, referred_id=referred_id))
    try:
        await session.commit()
    except IntegrityError:
        # Гонка двух параллельных /start: победил другой — это не ошибка.
        await session.rollback()
        return False
    return True


async def invited_count(player_id: int) -> int:
    """Сколько игроков пришло по личной ссылке."""
    async with SessionLocal() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(Referral)
            .where(Referral.referrer_id == player_id)
        )
        return int(total or 0)


async def referral_pot_balance(player_id: int) -> int:
    """Накопленная реферальная награда игрока в нанотонах (0 — ещё не копилось)."""
    async with SessionLocal() as session:
        value = await session.scalar(
            select(ReferralPot.nanotons).where(ReferralPot.referrer_id == player_id)
        )
        return int(value or 0)


async def resolve_bot_username(bot) -> str | None:
    """Username бота для ссылки: настройка приоритетнее, иначе get_me один
    раз (кэш на процесс). None — строить ссылку нечем."""
    global _BOT_USERNAME_CACHE
    if settings.bot_username.strip():
        return settings.bot_username.strip().lstrip("@")
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    if bot is None:
        return None
    try:
        me = await bot.get_me()
    except Exception:
        return None
    name = (getattr(me, "username", "") or "").strip().lstrip("@")
    if name:
        _BOT_USERNAME_CACHE = name
        return name
    return None