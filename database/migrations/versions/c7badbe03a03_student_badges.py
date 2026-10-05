"""student_badges: earned Bloom badges

Revision ID: c7badbe03a03
Revises: b3d9e1f04a7c
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7badbe03a03'
down_revision: Union[str, None] = 'b3d9e1f04a7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'student_badges',
        sa.Column('student_id', sa.UUID(), nullable=False),
        sa.Column('badge_id', sa.String(), nullable=False),
        sa.Column('earned_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['users.id']),
        sa.PrimaryKeyConstraint('student_id', 'badge_id'),
    )


def downgrade() -> None:
    op.drop_table('student_badges')
