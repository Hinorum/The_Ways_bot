"""add network to stakes tx_hash unique constraint

Revision ID: 1ac1c0a5612e
Revises: 73b01068e8bc
Create Date: 2026-09-05 10:00:47.561934

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1ac1c0a5612e'
down_revision: Union[str, Sequence[str], None] = '73b01068e8bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace stakes.tx_hash single-column unique with composite (tx_hash, network)."""
    with op.batch_alter_table("stakes") as batch:
        batch.drop_constraint("uq_stakes_tx_hash", type_="unique")
        batch.create_unique_constraint("uq_stake_tx_network", ["tx_hash", "network"])


def downgrade() -> None:
    """Revert to single-column unique on stakes.tx_hash."""
    with op.batch_alter_table("stakes") as batch:
        batch.drop_constraint("uq_stake_tx_network", type_="unique")
        batch.create_unique_constraint("uq_stakes_tx_hash", ["tx_hash"])
