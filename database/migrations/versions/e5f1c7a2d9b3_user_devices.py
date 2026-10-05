"""user devices (Expo push tokens)

Revision ID: e5f1c7a2d9b3
Revises: c5e7a2b9d413
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f1c7a2d9b3'
down_revision: Union[str, None] = 'c5e7a2b9d413'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_devices',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('expo_push_token', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('expo_push_token'),
    )
    op.create_index('ix_user_devices_user_id', 'user_devices', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_user_devices_user_id', table_name='user_devices')
    op.drop_table('user_devices')
