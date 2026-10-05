"""voice test questions, per-topic scores, session records; drop schools.voice_pipeline

Revision ID: b3d9e1f04a7c
Revises: 147224db116f
Create Date: 2026-10-05 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b3d9e1f04a7c'
down_revision: Union[str, None] = '147224db116f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_bloom_level = postgresql.ENUM(name='bloom_level', create_type=False)
_question_status = postgresql.ENUM('DRAFT', 'APPROVED', name='voice_test_question_status')


def upgrade() -> None:
    _question_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        'voice_test_questions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('voice_test_id', sa.UUID(), nullable=False),
        sa.Column('order', sa.Integer(), nullable=False),
        sa.Column('dataset_question_id', sa.UUID(), nullable=True),
        sa.Column('bloom_level', _bloom_level, nullable=False),
        sa.Column('topic_id', sa.UUID(), nullable=True),
        sa.Column('subtopic_id', sa.UUID(), nullable=True),
        sa.Column('concepts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('original_text', sa.Text(), nullable=False),
        sa.Column('reworded_text', sa.Text(), nullable=False),
        sa.Column('spoken_text', sa.Text(), nullable=False),
        sa.Column('expected_points', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('misconceptions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('hints', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', postgresql.ENUM(name='voice_test_question_status', create_type=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['voice_test_id'], ['voice_tests.id']),
        sa.ForeignKeyConstraint(['dataset_question_id'], ['dataset_questions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['topic_id'], ['topics.id']),
        sa.ForeignKeyConstraint(['subtopic_id'], ['subtopics.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('voice_test_id', 'order'),
    )
    op.create_table(
        'student_test_result_topic_scores',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('student_test_result_id', sa.UUID(), nullable=False),
        sa.Column('topic_id', sa.UUID(), nullable=False),
        sa.Column('percent', sa.Integer(), nullable=False),
        sa.Column('answered', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['student_test_result_id'], ['student_test_results.id']),
        sa.ForeignKeyConstraint(['topic_id'], ['topics.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_test_result_id', 'topic_id'),
    )
    op.create_table(
        'ai_assessor_session_records',
        sa.Column('session_id', sa.UUID(), nullable=False),
        sa.Column('transcript', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('needs_review', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['ai_assessor_sessions.id']),
        sa.PrimaryKeyConstraint('session_id'),
    )
    # Realtime is the only voice pipeline now - nothing to choose per school.
    op.drop_column('schools', 'voice_pipeline')


def downgrade() -> None:
    op.add_column('schools', sa.Column('voice_pipeline', sa.String(), nullable=True))
    op.drop_table('ai_assessor_session_records')
    op.drop_table('student_test_result_topic_scores')
    op.drop_table('voice_test_questions')
    _question_status.drop(op.get_bind(), checkfirst=True)
