"""rounds sealed day flag

Revision ID: e5f9b3c27d81
Revises: c8d2e5a71b94
Create Date: 2026-08-24 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f9b3c27d81'
down_revision: Union[str, Sequence[str], None] = 'c8d2e5a71b94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('rounds', sa.Column('sealed', sa.Boolean(), nullable=False, server_default=sa.text('0')))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('rounds', 'sealed')
