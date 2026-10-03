"""homework generation marker

Revision ID: d419ac55d5ef
Revises: 285c991419a6
Create Date: 2026-09-25 14:58:30.475844

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd419ac55d5ef'
down_revision: Union[str, None] = '285c991419a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('homework', sa.Column('generation_started_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('homework', 'generation_started_at')
