"""rounds season and place

Revision ID: a3f8c1d92e47
Revises: dc55f0bdf07d
Create Date: 2026-08-23 19:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f8c1d92e47'
down_revision: Union[str, Sequence[str], None] = 'dc55f0bdf07d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('rounds', sa.Column('season', sa.String(length=7), nullable=True))
    op.create_index('ix_rounds_season', 'rounds', ['season'])
    op.add_column('rounds', sa.Column('place', sa.String(length=80), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('rounds', 'place')
    op.drop_index('ix_rounds_season', table_name='rounds')
    op.drop_column('rounds', 'season')
