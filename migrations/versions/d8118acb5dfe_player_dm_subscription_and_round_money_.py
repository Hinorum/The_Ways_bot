"""players.dm_subscribed и rounds.money_mode: личная рассылка + версия игры.

Личные дубликаты анонсов (итоги дня, новый день с обложкой, вечерний пост)
идут подписанным игрокам — флаг подписки живёт на игроке и управляется из
/start (кнопка включения/выключения).

Round.money_mode — снимок «версии игры» (со ставками / без) на момент
открытия дня: хранитель переключает рубильник из /panel, вступает со
следующего дня, а текущий день живёт по своему снимку.

Revision ID: d8118acb5dfe
Revises: d6e8a4f0c1b9
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d8118acb5dfe"
down_revision: Union[str, Sequence[str], None] = "d6e8a4f0c1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Добавляет флаги: подписка игрока на личку и режим денежного дня."""
    op.add_column(
        "players",
        sa.Column("dm_subscribed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "rounds",
        sa.Column("money_mode", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    """Убирает флаги: существующие строки возвращаются к состоянию по умолчанию."""
    op.drop_column("rounds", "money_mode")
    op.drop_column("players", "dm_subscribed")