"""Paper Copilot: question_paper_questions metadata + source, question_paper_versions

Revision ID: c1f5a9e3b7d2
Revises: b4d8e2f6a1c9
Create Date: 2026-10-07 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c1f5a9e3b7d2'
down_revision: Union[str, None] = 'b4d8e2f6a1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_QUESTION_COLUMNS = (
    ('question_type', postgresql.ENUM(name='question_type', create_type=False)),
    ('difficulty', postgresql.ENUM(name='paper_difficulty_level', create_type=False)),
    ('chapter_id', postgresql.UUID(as_uuid=True)),
    ('topic_id', postgresql.UUID(as_uuid=True)),
    ('section_name', sa.String()),
    ('options', postgresql.JSONB()),
    ('answer', sa.Text()),
    ('dataset_question_id', postgresql.UUID(as_uuid=True)),
)


def upgrade() -> None:
    for name, type_ in _QUESTION_COLUMNS:
        op.add_column('question_paper_questions', sa.Column(name, type_, nullable=True))
    # every question that exists today came out of generation
    op.add_column('question_paper_questions', sa.Column('source', sa.String(), nullable=True, server_default=sa.text("'ai'")))
    op.create_foreign_key('fk_qpq_chapter_id', 'question_paper_questions', 'chapters', ['chapter_id'], ['id'])
    op.create_foreign_key('fk_qpq_topic_id', 'question_paper_questions', 'topics', ['topic_id'], ['id'])
    op.create_foreign_key('fk_qpq_dataset_question_id', 'question_paper_questions', 'dataset_questions',
                          ['dataset_question_id'], ['id'], ondelete='SET NULL')

    op.create_table(
        'question_paper_versions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('paper_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('question_papers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('origin', sa.String(), nullable=False),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('snapshot', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.UniqueConstraint('paper_id', 'number'),
    )
    op.create_index('ix_question_paper_versions_paper_id', 'question_paper_versions', ['paper_id'])


def downgrade() -> None:
    op.drop_index('ix_question_paper_versions_paper_id', table_name='question_paper_versions')
    op.drop_table('question_paper_versions')
    for name in ('fk_qpq_dataset_question_id', 'fk_qpq_topic_id', 'fk_qpq_chapter_id'):
        op.drop_constraint(name, 'question_paper_questions', type_='foreignkey')
    op.drop_column('question_paper_questions', 'source')
    for name, _ in reversed(_QUESTION_COLUMNS):
        op.drop_column('question_paper_questions', name)
