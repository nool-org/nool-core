"""Per-school module switches: schools.disabled_features (Feature values a Super
Admin switched off for the school, on top of its plan). NULL = nothing switched off.

Revision ID: e2a4c6d8f0b1
Revises: d1f3b5c7e9a2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = 'e2a4c6d8f0b1'
down_revision: Union[str, None] = 'd1f3b5c7e9a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('schools', sa.Column('disabled_features', JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('schools', 'disabled_features')
