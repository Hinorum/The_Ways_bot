from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import WinRule

from .time import _now, utc_aware

logger = logging.getLogger(__name__)

# Формат payload дна. v4: инлайн-день — глава, карты и обложка рендерятся
# сразу целиком. После удаления сюжетного слоя обложка не генерируется
# (cover_path=""), но маркер формата остаётся паспортом для материализации.
PREPARED_PAYLOAD_VERSION = 4

# Шаблон дня: без нейросети и арта день жил бы пустым. Три постоянные дороги,
# по которым стая голосует банком, — механика (счёт, жребий, выплаты) не
# зависит от их названия.
_TEMPLATE_CARDS: list[dict] = [
    {
        "position": 0,
        "title": "Путь I",
        "description": "Первая дорога для голоса стаи.",
        "consequence": "Стая выбрала первую дорогу.",
        "tag": "care",
        "image_path": "",
    },
    {
        "position": 1,
        "title": "Путь II",
        "description": "Вторая дорога для голоса стаи.",
        "consequence": "Стaya выбрала вторую дорогу.",
        "tag": "care",
        "image_path": "",
    },
    {
        "position": 2,
        "title": "Путь III",
        "description": "Третья дорога для голоса стаи.",
        "consequence": "Стaya выбрала третью дорогу.",
        "tag": "care",
        "image_path": "",
    },
]


def commit_rule(rule: WinRule, salt: str) -> str:
    return hashlib.sha256(f"{rule.value}:{salt}".encode()).hexdigest()


def _season_key(moment: datetime) -> str:
    """Ключ сезона (месяц UTC) для Round.season; раньше — app.season.season_key."""
    return f"{moment.year:04d}-{moment.month:02d}"


def _day_window(opens_at: datetime) -> tuple[datetime, datetime]:
    """Границы голосования и подсчёта дня — те же, что раньше строил day_context."""
    from datetime import timedelta

    voting_ends_at = opens_at + timedelta(seconds=settings.round_seconds)
    tally_ends_at = voting_ends_at + timedelta(seconds=settings.tally_seconds)
    return voting_ends_at, tally_ends_at


async def _plan_and_render(
    session: AsyncSession,
    day_index: int,
    opens_hint: datetime | None = None,
) -> dict:
    """Собирает день без сюжета: заголовок, три дороги и запечатанный закон.

    Сеть не трогается: никакой главы, арта и библии. Механика дня (банк,
    голоса TON, жребий по обязательству, выплаты) работает на этом шаблоне.
    """
    now = _now()
    opens_at = (
        now
        if opens_hint is None
        else max(now, utc_aware(opens_hint))
    )
    salt = secrets.token_hex(16)
    rng = secrets.SystemRandom()
    rule = rng.choice(list(WinRule))
    cards_payload = [dict(card) for card in _TEMPLATE_CARDS]
    chapter_text = (
        f"День {day_index}. Племя собирается у костра — сегодня дорогу выбирает "
        "голос стаи: каждый бросает свой голос за одну из трёх троп."
    )
    return {
        "v": PREPARED_PAYLOAD_VERSION,
        "day_index": day_index,
        "rule": rule.value,
        "commitment": commit_rule(rule, salt) + ":" + salt,
        "sealed": False,
        "chapter_title": f"День {day_index}",
        "chapter_text": chapter_text,
        "lore_summary": "",
        "place": None,
        "season": _season_key(opens_at),
        "cover_path": "",
        "cards": cards_payload,
    }