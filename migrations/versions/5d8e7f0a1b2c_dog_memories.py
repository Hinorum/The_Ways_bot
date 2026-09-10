"""add dog_memories table

Revision ID: 5d8e7f0a1b2c
Revises: 1ac1c0a5612e
Create Date: 2026-09-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5d8e7f0a1b2c"
down_revision: Union[str, Sequence[str], None] = "1ac1c0a5612e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dog_memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dog_key", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), server_default="birth", nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("scar_key", sa.String(64), server_default="", nullable=False),
        sa.Column("created_day", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), server_default="suppressed", nullable=False),
        sa.Column("surfaced_day", sa.Integer(), nullable=True),
        sa.Column("healed_day", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dog_memories_dog_key", "dog_memories", ["dog_key"])
    op.create_index("ix_dog_memories_scar_key", "dog_memories", ["scar_key"])
    op.create_index("ix_dog_memories_created_day", "dog_memories", ["created_day"])


def downgrade() -> None:
    op.drop_index("ix_dog_memories_created_day")
    op.drop_index("ix_dog_memories_scar_key")
    op.drop_index("ix_dog_memories_dog_key")
    op.drop_table("dog_memories")