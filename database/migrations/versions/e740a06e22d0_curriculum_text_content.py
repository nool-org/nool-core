"""curriculum text content

Revision ID: e740a06e22d0
Revises: c8ed4d43cba0
Create Date: 2026-09-25 04:19:54.405765

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e740a06e22d0'
down_revision: Union[str, None] = 'c8ed4d43cba0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subtopics', sa.Column('text_content', sa.Text(), nullable=True))
    op.add_column('topics', sa.Column('text_content', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('topics', 'text_content')
    op.drop_column('subtopics', 'text_content')
