"""payouts.last_error: причина последней неудачи отправки выплаты.

Revision ID: f7a2c9d41b63
Revises: e5f9b3c27d81
Create Date: 2026-08-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a2c9d41b63"
down_revision: Union[str, Sequence[str], None] = "e5f9b3c27d81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("payouts", sa.Column("last_error", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("payouts", "last_error")
