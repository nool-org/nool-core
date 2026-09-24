"""subject grades

Revision ID: c4a1f0d6b8e2
Revises: 012bd47f8e6b
Create Date: 2026-09-23 00:00:00.000000

Which grades a Subject applies to - a global catalog fact, not a
per-school assignment. See SubjectGrade's model docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4a1f0d6b8e2'
down_revision: Union[str, None] = '012bd47f8e6b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'subject_grades',
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('grade', sa.Integer(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subject_id', 'grade'),
    )


def downgrade() -> None:
    op.drop_table('subject_grades')
