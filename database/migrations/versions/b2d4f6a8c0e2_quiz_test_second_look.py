"""A teacher's Fix Milo challenge gets a second look, like a check-in: after
the homework, the student re-takes it with fresh Fix Milo problems
(attempt 2). One attempt per student, test and attempt number.

Revision ID: b2d4f6a8c0e2
Revises: a1c3e5f7b9d1
Create Date: 2026-10-13 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2d4f6a8c0e2'
down_revision: Union[str, None] = 'a1c3e5f7b9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('uq_quiz_test_attempts_student_test', table_name='daily_practice_sessions')
    op.create_index('uq_quiz_test_attempts_student_test', 'daily_practice_sessions',
                    ['student_id', 'quiz_test_id', 'attempt'], unique=True,
                    postgresql_where=sa.text('quiz_test_id IS NOT NULL'))


def downgrade() -> None:
    op.execute("DELETE FROM daily_practice_sessions WHERE quiz_test_id IS NOT NULL AND attempt > 1")
    op.drop_index('uq_quiz_test_attempts_student_test', table_name='daily_practice_sessions')
    op.create_index('uq_quiz_test_attempts_student_test', 'daily_practice_sessions',
                    ['student_id', 'quiz_test_id'], unique=True,
                    postgresql_where=sa.text('quiz_test_id IS NOT NULL'))
