"""per-alert notification preferences

Revision ID: d7e9a1c3b5f8
Revises: c8a2e4f6b1d3
Create Date: 2026-10-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e9a1c3b5f8'
down_revision: Union[str, None] = 'c8a2e4f6b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_notification_prefs',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('results_ready', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('answers_to_review', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('daily_summary', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id'),
    )


def downgrade() -> None:
    op.drop_table('user_notification_prefs')
