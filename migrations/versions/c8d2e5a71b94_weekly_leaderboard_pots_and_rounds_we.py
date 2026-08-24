"""weekly leaderboard pots and rounds weekly cut

Revision ID: c8d2e5a71b94
Revises: a3f8c1d92e47
Create Date: 2026-08-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'c8d2e5a71b94'
down_revision: Union[str, Sequence[str], None] = 'a3f8c1d92e47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'weekly_pots',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('week', sa.String(length=16), nullable=False),
        sa.Column('nanotons', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('week'),
    )
    op.add_column('rounds', sa.Column('weekly_nanotons', sa.BigInteger(), nullable=False, server_default='0'))
    # memory_hits появилась в моделях после baseline и жила только через
    # create_all: создаём под guard'ом, чтобы не упасть на живых базах.
    if not inspect(op.get_bind()).has_table('memory_hits'):
        op.create_table(
            'memory_hits',
            sa.Column('player_id', sa.BigInteger(), nullable=False),
            sa.Column('round_id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.PrimaryKeyConstraint('player_id', 'round_id'),
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('rounds', 'weekly_nanotons')
    op.drop_table('weekly_pots')
