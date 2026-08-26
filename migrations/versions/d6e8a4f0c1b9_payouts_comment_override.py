"""payouts.comment_override: свободный комментарий исходящего перевода.

Revision ID: d6e8a4f0c1b9
Revises: b8f3e2a6c9d7
Create Date: 2026-08-26

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6e8a4f0c1b9"
down_revision: Union[str, Sequence[str], None] = "b8f3e2a6c9d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("payouts", sa.Column("comment_override", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("payouts", "comment_override")
