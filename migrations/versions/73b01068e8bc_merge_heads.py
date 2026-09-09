"""merge heads

Revision ID: 73b01068e8bc
Revises: 4e5f6a7b8c9d, b8c9d0e1f2a3
Create Date: 2026-09-05 10:00:04.855568

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '73b01068e8bc'
down_revision: Union[str, Sequence[str], None] = ('4e5f6a7b8c9d', 'b8c9d0e1f2a3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
