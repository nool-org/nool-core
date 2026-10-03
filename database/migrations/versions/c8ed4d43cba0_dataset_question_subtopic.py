"""dataset question subtopic

Revision ID: c8ed4d43cba0
Revises: dc32431ec12d
Create Date: 2026-09-24 15:52:35.030077

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8ed4d43cba0'
down_revision: Union[str, None] = 'dc32431ec12d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('dataset_questions', sa.Column('subtopic_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_dataset_questions_subtopic_id', 'dataset_questions', 'subtopics', ['subtopic_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_dataset_questions_subtopic_id', 'dataset_questions', type_='foreignkey')
    op.drop_column('dataset_questions', 'subtopic_id')
