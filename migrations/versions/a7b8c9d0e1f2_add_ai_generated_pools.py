"""add ai_generated_pools table

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_generated_pools",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pool_type", sa.String(32), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(16), server_default="", nullable=False),
        sa.Column("content_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_generated_pools_pool_type", "ai_generated_pools", ["pool_type"])
    op.create_index("ix_ai_generated_pools_season", "ai_generated_pools", ["season"])


def downgrade() -> None:
    op.drop_index("ix_ai_generated_pools_season")
    op.drop_index("ix_ai_generated_pools_pool_type")
    op.drop_table("ai_generated_pools")
