"""призвания, вдохновение и бестиарий: players.calling/.inspiration + bestiary_sightings

Revision ID: b8f3e2a6c9d7
Revises: c4e9b1a7d2f5
Create Date: 2026-08-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8f3e2a6c9d7"
down_revision: Union[str, Sequence[str], None] = "c4e9b1a7d2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("players", sa.Column("calling", sa.String(length=32), nullable=True))
    op.add_column(
        "players",
        sa.Column("inspiration", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "bestiary_sightings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("season", sa.String(length=16), nullable=False),
        sa.Column("beast_key", sa.String(length=32), nullable=False),
        sa.Column("day_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("season", "beast_key", name="uq_bestiary_season_beast"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("bestiary_sightings")
    op.drop_column("players", "inspiration")
    op.drop_column("players", "calling")
