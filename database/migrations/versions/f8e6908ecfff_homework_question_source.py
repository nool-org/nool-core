"""homework question source

Revision ID: f8e6908ecfff
Revises: e740a06e22d0
Create Date: 2026-09-25 04:30:47.855731

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8e6908ecfff'
down_revision: Union[str, None] = 'e740a06e22d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('homework_questions', sa.Column('source_question_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_homework_questions_source_question_id', 'homework_questions', 'dataset_questions',
                          ['source_question_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_homework_questions_source_question_id', 'homework_questions', type_='foreignkey')
    op.drop_column('homework_questions', 'source_question_id')
