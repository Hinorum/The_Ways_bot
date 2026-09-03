"""add npc_motives table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "npc_motives",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("npc_key", sa.String(32), nullable=False),
        sa.Column("mood", sa.String(16), nullable=False),
        sa.Column("motive_text", sa.Text(), nullable=False),
        sa.Column("action_text", sa.Text(), nullable=False),
        sa.Column("thought_pool_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_npc_motives_npc_key", "npc_motives", ["npc_key"])


def downgrade() -> None:
    op.drop_index("ix_npc_motives_npc_key")
    op.drop_table("npc_motives")
