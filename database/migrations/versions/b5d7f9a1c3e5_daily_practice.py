"""Daily practice: chapter progress, per-topic mastery, a reusable question pool,
and each student's daily session (MCQs + recorded voice answers)

Revision ID: b5d7f9a1c3e5
Revises: a4c6e8b0d2f4
Create Date: 2026-10-11 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b5d7f9a1c3e5'
down_revision: Union[str, None] = 'a4c6e8b0d2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BLOOM = postgresql.ENUM(name='bloom_level', create_type=False)


def upgrade() -> None:
    op.create_table(
        'class_topic_progress',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('class_id', sa.UUID(), sa.ForeignKey('classes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', sa.UUID(), sa.ForeignKey('chapters.id'), nullable=False),
        sa.Column('topic_id', sa.UUID(), sa.ForeignKey('topics.id'), nullable=False),
        sa.Column('taught_on', sa.Date(), nullable=False),
        sa.Column('marked_by', sa.UUID(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('class_id', 'topic_id', name='uq_class_topic_progress_class_topic'),
    )
    op.create_table(
        'student_topic_mastery',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('student_id', sa.UUID(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('topic_id', sa.UUID(), sa.ForeignKey('topics.id'), nullable=False),
        sa.Column('chapter_id', sa.UUID(), sa.ForeignKey('chapters.id'), nullable=False),
        sa.Column('mastery', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('correct', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_practised_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'topic_id', name='uq_student_topic_mastery_student_topic'),
    )
    op.create_table(
        'practice_questions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('chapter_id', sa.UUID(), sa.ForeignKey('chapters.id'), nullable=False),
        sa.Column('topic_id', sa.UUID(), sa.ForeignKey('topics.id'), nullable=True),
        sa.Column('bloom_level', BLOOM, nullable=False),
        sa.Column('kind', sa.String(10), nullable=False),  # mcq | voice
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('options', postgresql.JSONB(), nullable=True),
        sa.Column('correct_index', sa.Integer(), nullable=True),
        sa.Column('explanation', sa.Text(), nullable=True),
        sa.Column('hint', sa.Text(), nullable=True),
        sa.Column('expected_points', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('misconceptions', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('source_question_id', sa.UUID(),
                  sa.ForeignKey('dataset_questions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('origin', sa.String(20), nullable=False),  # bank | generated
        sa.Column('used_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_practice_questions_pool', 'practice_questions', ['chapter_id', 'topic_id', 'kind'])
    op.create_table(
        'daily_practice_sessions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('student_id', sa.UUID(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('practice_date', sa.Date(), nullable=False),
        sa.Column('chapter_id', sa.UUID(), sa.ForeignKey('chapters.id'), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='open'),  # open | completed
        sa.Column('xp_awarded', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'practice_date', name='uq_daily_practice_sessions_student_date'),
    )
    op.create_table(
        'daily_practice_items',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('session_id', sa.UUID(), sa.ForeignKey('daily_practice_sessions.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('order', sa.Integer(), nullable=False),
        sa.Column('practice_question_id', sa.UUID(), sa.ForeignKey('practice_questions.id'), nullable=False),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('topic_id', sa.UUID(), sa.ForeignKey('topics.id'), nullable=True),
        sa.Column('bloom_level', BLOOM, nullable=False),
        sa.Column('review', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('option_index', sa.Integer(), nullable=True),
        sa.Column('correct', sa.Boolean(), nullable=True),
        sa.Column('transcript', sa.Text(), nullable=True),
        sa.Column('score', sa.Integer(), nullable=True),
        sa.Column('feedback', postgresql.JSONB(), nullable=True),  # {"good": str, "add": str}
        sa.Column('elapsed_ms', sa.Integer(), nullable=True),
        sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('xp_awarded', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', 'order', name='uq_daily_practice_items_session_order'),
    )


def downgrade() -> None:
    op.drop_table('daily_practice_items')
    op.drop_table('daily_practice_sessions')
    op.drop_index('ix_practice_questions_pool', table_name='practice_questions')
    op.drop_table('practice_questions')
    op.drop_table('student_topic_mastery')
    op.drop_table('class_topic_progress')
