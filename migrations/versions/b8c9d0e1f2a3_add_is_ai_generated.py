"""add is_ai_generated column for regeneration tracking

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ai_generated_pools", sa.Column("is_ai_generated", sa.Boolean(), server_default=sa.text("0"), nullable=False))
    op.add_column("npc_profiles", sa.Column("is_ai_generated", sa.Boolean(), server_default=sa.text("0"), nullable=False))
    op.add_column("prologue_beats", sa.Column("is_ai_generated", sa.Boolean(), server_default=sa.text("0"), nullable=False))
    op.add_column("season_arcs", sa.Column("is_ai_generated", sa.Boolean(), server_default=sa.text("0"), nullable=False))


def downgrade() -> None:
    op.drop_column("season_arcs", "is_ai_generated")
    op.drop_column("prologue_beats", "is_ai_generated")
    op.drop_column("npc_profiles", "is_ai_generated")
    op.drop_column("ai_generated_pools", "is_ai_generated")
