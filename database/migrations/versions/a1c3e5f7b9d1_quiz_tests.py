"""Teacher-made written tests (quiz tests) - for subjects without voice
tests (Maths): a fixed set of practice-pool questions (MCQ, who_is_right,
fix_steps, make_your_own) the teacher picked the mix of, published to a
class. A student's attempt is a practice session with quiz_test_id, so it
reuses the topic quiz's answering, analysis and homework.

Revision ID: a1c3e5f7b9d1
Revises: f9b1d3e5a7c9
Create Date: 2026-10-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a1c3e5f7b9d1'
down_revision: Union[str, None] = 'f9b1d3e5a7c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'quiz_tests',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('class_id', sa.UUID(), sa.ForeignKey('classes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('subject_id', sa.UUID(), sa.ForeignKey('subjects.id'), nullable=False),
        sa.Column('chapter_id', sa.UUID(), sa.ForeignKey('chapters.id'), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('topic_ids', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('mix', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('question_ids', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('status', sa.String(20), nullable=False),  # preparing | ready | failed | published
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.UUID(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_quiz_tests_class', 'quiz_tests', ['class_id'])

    t = 'daily_practice_sessions'
    op.add_column(t, sa.Column('quiz_test_id', sa.UUID(), sa.ForeignKey('quiz_tests.id', ondelete='CASCADE'),
                               nullable=True))
    # the day's own practice is the session with neither a topic nor a test
    op.drop_index('uq_daily_practice_sessions_student_date', t)
    op.create_index('uq_daily_practice_sessions_student_date', t, ['student_id', 'practice_date'], unique=True,
                    postgresql_where=sa.text('topic_id IS NULL AND quiz_test_id IS NULL'))
    op.create_index('uq_quiz_test_attempts_student_test', t, ['student_id', 'quiz_test_id'], unique=True,
                    postgresql_where=sa.text('quiz_test_id IS NOT NULL'))


def downgrade() -> None:
    t = 'daily_practice_sessions'
    op.execute(f"DELETE FROM {t} WHERE quiz_test_id IS NOT NULL")
    op.drop_index('uq_quiz_test_attempts_student_test', t)
    op.drop_index('uq_daily_practice_sessions_student_date', t)
    op.create_index('uq_daily_practice_sessions_student_date', t, ['student_id', 'practice_date'], unique=True,
                    postgresql_where=sa.text('topic_id IS NULL'))
    op.drop_column(t, 'quiz_test_id')
    op.drop_index('ix_quiz_tests_class', 'quiz_tests')
    op.drop_table('quiz_tests')
