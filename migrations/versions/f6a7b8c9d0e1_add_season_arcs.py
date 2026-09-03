"""add season_arcs table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "season_arcs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("stage_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("tone", sa.Text(), server_default="", nullable=False),
        sa.Column("missions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("whisper_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("teaser_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("guest", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_season_arcs_season", "season_arcs", ["season"])


def downgrade() -> None:
    op.drop_index("ix_season_arcs_season")
    op.drop_table("season_arcs")
