"""fix: add CHECK constraints for status validation

Revision ID: 4e5f6a7b8c9d
Revises: 3f8a1b2c9d0e
Create Date: 2026-09-04 12:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e5f6a7b8c9d'
down_revision: Union[str, Sequence[str], None] = '3f8a1b2c9d0e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add CHECK constraints for status validation (idempotent)."""
    bind = op.get_bind()
    
    # Only add constraints if they don't exist
    if bind.dialect.name == 'postgresql':
        # Check if ck_stake_valid_status exists
        result = bind.execute(sa.text("""
            SELECT conname FROM pg_constraint 
            WHERE conname = 'ck_stake_valid_status'
        """))
        if not result.fetchone():
            op.create_check_constraint(
                'ck_stake_valid_status', 'stakes',
                "status IN ('pending', 'confirmed', 'rejected', 'refunded')"
            )
        
        # Check if ck_payout_valid_status exists
        result = bind.execute(sa.text("""
            SELECT conname FROM pg_constraint 
            WHERE conname = 'ck_payout_valid_status'
        """))
        if not result.fetchone():
            op.create_check_constraint(
                'ck_payout_valid_status', 'payouts',
                "status IN ('pending', 'sending', 'sent', 'failed', 'dismissed')"
            )


def downgrade() -> None:
    """Remove CHECK constraints for status validation."""
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        try:
            op.drop_constraint('ck_stake_valid_status', 'stakes', type_='check')
        except Exception:
            pass
        try:
            op.drop_constraint('ck_payout_valid_status', 'payouts', type_='check')
        except Exception:
            pass
