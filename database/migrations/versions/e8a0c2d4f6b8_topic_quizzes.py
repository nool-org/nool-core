"""Topic quizzes: per-topic MCQ quiz -> analysis -> homework -> re-check quiz.
A topic quiz is a practice session with a topic and an attempt (1 = quiz,
2 = re-check), so it reuses daily practice's items, answering and mastery.
Homework can now come from a topic quiz instead of a voice test.

Revision ID: e8a0c2d4f6b8
Revises: d7f9b1c3e5a7
Create Date: 2026-10-12 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e8a0c2d4f6b8'
down_revision: Union[str, None] = 'd7f9b1c3e5a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('homework', sa.Column('topic_id', sa.UUID(), sa.ForeignKey('topics.id'), nullable=True))
    op.alter_column('homework', 'test_id', nullable=True)

    t = 'daily_practice_sessions'
    op.add_column(t, sa.Column('topic_id', sa.UUID(), sa.ForeignKey('topics.id'), nullable=True))
    op.add_column(t, sa.Column('attempt', sa.SmallInteger(), nullable=True))
    op.add_column(t, sa.Column('score_percent', sa.Integer(), nullable=True))
    op.add_column(t, sa.Column('per_bloom', postgresql.JSONB(), nullable=True))
    op.add_column(t, sa.Column('analysis', postgresql.JSONB(), nullable=True))
    op.add_column(t, sa.Column('outcome', sa.String(20), nullable=True))  # homework | mastered | needs_teacher
    op.add_column(t, sa.Column('homework_id', sa.UUID(), sa.ForeignKey('homework.id', ondelete='SET NULL'),
                               nullable=True))
    op.drop_constraint('uq_daily_practice_sessions_student_date', t, type_='unique')
    op.create_index('uq_daily_practice_sessions_student_date', t, ['student_id', 'practice_date'],
                    unique=True, postgresql_where=sa.text('topic_id IS NULL'))
    op.create_index('uq_topic_quizzes_student_topic_attempt', t, ['student_id', 'topic_id', 'attempt'],
                    unique=True, postgresql_where=sa.text('topic_id IS NOT NULL'))


def downgrade() -> None:
    t = 'daily_practice_sessions'
    op.execute(f"DELETE FROM {t} WHERE topic_id IS NOT NULL")
    op.drop_index('uq_topic_quizzes_student_topic_attempt', t)
    op.drop_index('uq_daily_practice_sessions_student_date', t)
    op.create_unique_constraint('uq_daily_practice_sessions_student_date', t, ['student_id', 'practice_date'])
    for col in ('homework_id', 'outcome', 'analysis', 'per_bloom', 'score_percent', 'attempt', 'topic_id'):
        op.drop_column(t, col)
    op.execute("DELETE FROM homework WHERE test_id IS NULL")
    op.alter_column('homework', 'test_id', nullable=False)
    op.drop_column('homework', 'topic_id')
