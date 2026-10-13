"""A Fix Milo challenge (co-learner for Maths) gets the same options as a
voice co-learner: a completion window counted from when it opens (the due
time is set when it's published), and whole class or chosen students.

Revision ID: c3e5a7b9d1f3
Revises: b2d4f6a8c0e2
Create Date: 2026-10-13 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3e5a7b9d1f3'
down_revision: Union[str, None] = 'b2d4f6a8c0e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('quiz_tests', sa.Column('completion_window_hours', sa.Integer(), nullable=True))
    # null = the whole class
    op.add_column('quiz_tests', sa.Column('student_ids', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('quiz_tests', 'student_ids')
    op.drop_column('quiz_tests', 'completion_window_hours')
