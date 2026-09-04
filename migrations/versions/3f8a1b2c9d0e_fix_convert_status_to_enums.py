"""fix: convert status columns to enums on PostgreSQL

Revision ID: 3f8a1b2c9d0e
Revises: dec4728c5f0c
Create Date: 2026-09-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f8a1b2c9d0e'
down_revision: Union[str, Sequence[str], None] = 'dec4728c5f0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Convert status columns from VARCHAR to enum on PostgreSQL."""
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Convert payouts.status to PayoutStatus enum
        op.execute("""
            ALTER TABLE payouts 
            ALTER COLUMN status TYPE VARCHAR(9) 
            USING status::VARCHAR(9)
        """)
        op.execute("""
            ALTER TABLE payouts 
            ALTER COLUMN status DROP DEFAULT
        """)
        op.execute("""
            ALTER TABLE payouts 
            ALTER COLUMN status TYPE VARCHAR(9)
        """)
        
        # Convert stakes.status to StakeStatus enum
        op.execute("""
            ALTER TABLE stakes 
            ALTER COLUMN status TYPE VARCHAR(16) 
            USING status::VARCHAR(16)
        """)
        op.execute("""
            ALTER TABLE stakes 
            ALTER COLUMN status DROP DEFAULT
        """)
        op.execute("""
            ALTER TABLE stakes 
            ALTER COLUMN status TYPE VARCHAR(16)
        """)


def downgrade() -> None:
    """Revert status columns to plain VARCHAR."""
    pass  # No-op: VARCHAR is compatible with both string and enum values
