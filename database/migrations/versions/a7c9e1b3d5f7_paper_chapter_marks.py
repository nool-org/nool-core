"""question_paper_chapters.marks - the teacher's weightage: how many of the
paper's marks come from each chapter (null = no weightage set, the generator
spreads questions freely).

Revision ID: a7c9e1b3d5f7
Revises: f6b8d0a2c4e6
Create Date: 2026-10-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7c9e1b3d5f7'
down_revision: Union[str, None] = 'f6b8d0a2c4e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('question_paper_chapters', sa.Column('marks', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('question_paper_chapters', 'marks')
