"""pack_fund, pack_fund_ledger, disputes tables + payouts indexes

Revision ID: a1b2c3d4e5f6
Revises: 521436baf0ea
Create Date: 2026-08-31 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '521436baf0ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pack_fund',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('nanotons', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'pack_fund_ledger',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('entry_type', sa.String(8), nullable=False),
        sa.Column('amount_nanotons', sa.BigInteger(), nullable=False),
        sa.Column('round_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_pack_fund_ledger_round_id'), 'pack_fund_ledger', ['round_id'], unique=False)
    op.create_table(
        'disputes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('round_id', sa.Integer(), nullable=True),
        sa.Column('player_id', sa.Integer(), nullable=True),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('keeper_note', sa.String(300), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_disputes_round_id'), 'disputes', ['round_id'], unique=False)
    op.create_index(op.f('ix_disputes_player_id'), 'disputes', ['player_id'], unique=False)
    op.create_index(op.f('ix_payouts_status'), 'payouts', ['status'], unique=False)
    op.create_index(op.f('ix_payouts_player_id'), 'payouts', ['player_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_payouts_player_id'), table_name='payouts')
    op.drop_index(op.f('ix_payouts_status'), table_name='payouts')
    op.drop_index(op.f('ix_disputes_player_id'), table_name='disputes')
    op.drop_index(op.f('ix_disputes_round_id'), table_name='disputes')
    op.drop_table('disputes')
    op.drop_index(op.f('ix_pack_fund_ledger_round_id'), table_name='pack_fund_ledger')
    op.drop_table('pack_fund_ledger')
    op.drop_table('pack_fund')
