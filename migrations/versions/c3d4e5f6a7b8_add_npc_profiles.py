"""add npc_profiles table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "npc_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("npc_key", sa.String(32), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("personality", sa.Text(), nullable=False),
        sa.Column("speech_style", sa.Text(), server_default="", nullable=False),
        sa.Column("appearance", sa.Text(), server_default="", nullable=False),
        sa.Column("default_mood", sa.String(16), server_default="neutral", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_npc_profiles_npc_key", "npc_profiles", ["npc_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_npc_profiles_npc_key")
    op.drop_table("npc_profiles")
