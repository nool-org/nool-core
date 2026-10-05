"""mcq tests and their questions

Revision ID: c5e7a2b9d413
Revises: b3d9e1f04a7c
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e7a2b9d413'
down_revision: Union[str, None] = 'b3d9e1f04a7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_bloom_level = postgresql.ENUM(name='bloom_level', create_type=False)


def upgrade() -> None:
    op.create_table(
        'mcq_tests',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('student_id', sa.UUID(), nullable=False),
        sa.Column('voice_test_id', sa.UUID(), nullable=False),
        sa.Column('unlocks_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('unlocked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('score_percent', sa.Integer(), nullable=True),
        sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['users.id']),
        sa.ForeignKeyConstraint(['voice_test_id'], ['voice_tests.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'voice_test_id'),
    )
    op.create_table(
        'mcq_test_questions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('mcq_test_id', sa.UUID(), nullable=False),
        sa.Column('order', sa.Integer(), nullable=False),
        sa.Column('bloom_level', _bloom_level, nullable=False),
        sa.Column('concept', sa.Text(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('correct_index', sa.Integer(), nullable=False),
        sa.Column('source_question_id', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['mcq_test_id'], ['mcq_tests.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_question_id'], ['dataset_questions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mcq_test_id', 'order'),
    )


def downgrade() -> None:
    op.drop_table('mcq_test_questions')
    op.drop_table('mcq_tests')
