"""question_paper_answer_keys + question_paper_parallels (answer key, Set B)

Revision ID: c5e1a7d3f9b2
Revises: b4d8e2f6a1c9
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e1a7d3f9b2'
down_revision: Union[str, None] = 'b4d8e2f6a1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'question_paper_answer_keys',
        sa.Column('question_id', sa.UUID(), nullable=False),
        sa.Column('answer', sa.Text(), nullable=False),
        sa.Column('marking_points', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_by', sa.UUID(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['question_id'], ['question_paper_questions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('question_id'),
    )
    op.create_table(
        'question_paper_parallels',
        sa.Column('paper_id', sa.UUID(), nullable=False),
        sa.Column('parallel_of', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['paper_id'], ['question_papers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['parallel_of'], ['question_papers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('paper_id'),
    )
    op.create_index(op.f('ix_question_paper_parallels_parallel_of'), 'question_paper_parallels', ['parallel_of'])


def downgrade() -> None:
    op.drop_index(op.f('ix_question_paper_parallels_parallel_of'), table_name='question_paper_parallels')
    op.drop_table('question_paper_parallels')
    op.drop_table('question_paper_answer_keys')
