"""add AI fields to consequence_branches

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("consequence_branches", sa.Column("title", sa.String(120), server_default="", nullable=False))
    op.add_column("consequence_branches", sa.Column("stage_text", sa.Text(), server_default="", nullable=False))
    op.add_column("consequence_branches", sa.Column("choices_json", sa.Text(), server_default="{}", nullable=False))
    op.add_column("consequence_branches", sa.Column("resolution", sa.Text(), server_default="", nullable=False))


def downgrade() -> None:
    op.drop_column("consequence_branches", "resolution")
    op.drop_column("consequence_branches", "choices_json")
    op.drop_column("consequence_branches", "stage_text")
    op.drop_column("consequence_branches", "title")
