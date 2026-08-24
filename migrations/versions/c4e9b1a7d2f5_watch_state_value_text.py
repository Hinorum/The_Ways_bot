"""watcher_state.value расширяется до Text: план злодея влезает целиком

План Хозяина Ошибки (3 канонических события сезона) переставал помещаться
в VARCHAR(255): INSERT падал StringDataRightTruncationError, тик умирал до
создания следующего дня, и мир останавливался на закрытом дне.

Revision ID: c4e9b1a7d2f5
Revises: f7a2c9d41b63
Create Date: 2026-08-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e9b1a7d2f5"
down_revision: Union[str, Sequence[str], None] = "f7a2c9d41b63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "watcher_state",
        "value",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=False,
        postgresql_using="value::text",
    )


def downgrade() -> None:
    op.alter_column(
        "watcher_state",
        "value",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
