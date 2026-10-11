"""MCQ game features and adaptive / self-improving voice questions

MCQ: per-question explanation and hint, a boss question, the voice-test gap
a question targets, per-question first answers (instant feedback), lifelines,
speed mode and a retry of the misses. Voice questions: an easier version per
slot (adaptive difficulty) and confusion counts across students (flags a
question for rewording).

Revision ID: a4c6e8b0d2f4
Revises: f1b3d5a7c9e2
Create Date: 2026-10-11 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a4c6e8b0d2f4'
down_revision: Union[str, None] = 'f1b3d5a7c9e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('mcq_test_questions', sa.Column('explanation', sa.Text(), nullable=True))
    op.add_column('mcq_test_questions', sa.Column('hint', sa.Text(), nullable=True))
    op.add_column('mcq_test_questions', sa.Column('is_boss', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('mcq_test_questions', sa.Column('gap_concept', sa.Text(), nullable=True))

    op.add_column('mcq_tests', sa.Column('lifelines_used', postgresql.JSONB(), nullable=False,
                                         server_default=sa.text("'[]'::jsonb")))
    op.add_column('mcq_tests', sa.Column('speed_mode', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('mcq_tests', sa.Column('retry_score_percent', sa.Integer(), nullable=True))
    op.add_column('mcq_tests', sa.Column('retry_completed_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        'mcq_question_answers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('mcq_test_question_id', sa.UUID(), nullable=False),
        sa.Column('student_id', sa.UUID(), nullable=False),
        sa.Column('option_index', sa.Integer(), nullable=False),
        sa.Column('correct', sa.Boolean(), nullable=False),
        sa.Column('elapsed_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['mcq_test_question_id'], ['mcq_test_questions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mcq_test_question_id', 'student_id', name='uq_mcq_question_answers_question_student'),
    )

    op.add_column('voice_test_questions', sa.Column('easier_version', postgresql.JSONB(), nullable=True))
    op.add_column('voice_test_questions', sa.Column('asked_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('voice_test_questions', sa.Column('confused_count', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('voice_test_questions', sa.Column('needs_rewording', sa.Boolean(), nullable=False,
                                                    server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('voice_test_questions', 'needs_rewording')
    op.drop_column('voice_test_questions', 'confused_count')
    op.drop_column('voice_test_questions', 'asked_count')
    op.drop_column('voice_test_questions', 'easier_version')
    op.drop_table('mcq_question_answers')
    op.drop_column('mcq_tests', 'retry_completed_at')
    op.drop_column('mcq_tests', 'retry_score_percent')
    op.drop_column('mcq_tests', 'speed_mode')
    op.drop_column('mcq_tests', 'lifelines_used')
    op.drop_column('mcq_test_questions', 'gap_concept')
    op.drop_column('mcq_test_questions', 'is_boss')
    op.drop_column('mcq_test_questions', 'hint')
    op.drop_column('mcq_test_questions', 'explanation')
