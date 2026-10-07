"""question_paper_templates (saved, school-shared paper blueprints)

Revision ID: c5e7a9d1f3b2
Revises: c5e1a7d3f9b2
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e7a9d1f3b2'
down_revision: Union[str, None] = 'c5e1a7d3f9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'question_paper_templates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('school_id', sa.UUID(), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('exam_type', sa.String(), nullable=False),
        sa.Column('board', sa.String(), nullable=False),
        sa.Column('grade', sa.Integer(), nullable=False),
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('total_marks', sa.Integer(), nullable=True),
        sa.Column('duration_minutes', sa.Integer(), nullable=True),
        sa.Column('blueprint', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_question_paper_templates_school_id', 'question_paper_templates', ['school_id'])


def downgrade() -> None:
    op.drop_index('ix_question_paper_templates_school_id', table_name='question_paper_templates')
    op.drop_table('question_paper_templates')
