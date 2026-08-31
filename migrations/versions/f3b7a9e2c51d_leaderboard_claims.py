"""leaderboard_claims: претензии на призовые места лидерборда.

Ничья в копилках недели/месяца решается кнопкой Claim в /start: кто раньше
нажал (в течение периода), тот выше при равенстве верных путей и вклада Gram.
unique(player_id, kind, period) делает претензию строго идемпотентной.

Применено к текущим дверям: месяц = top-K (по умолчанию топ-3), неделя = топ-3
по верным путям — обе с вкладом Gram во втором и Claim в третьем тай-брейке.

Revision ID: f3b7a9e2c51d
Revises: d8118acb5dfe
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3b7a9e2c51d"
down_revision: Union[str, Sequence[str], None] = "d8118acb5dfe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Добавляет таблицу претензий на места лидербордов."""
    op.create_table(
        "leaderboard_claims",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "player_id", "kind", "period", name="uq_leaderboard_claims_player_kind_period"
        ),
    )


def downgrade() -> None:
    """Убирает претензии: копилки возвращаются к решению ничьих по player_id."""
    op.drop_table("leaderboard_claims")