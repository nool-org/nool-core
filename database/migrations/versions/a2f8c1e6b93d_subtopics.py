"""subtopics

Revision ID: a2f8c1e6b93d
Revises: d7e5c2a9f014
Create Date: 2026-09-23 00:00:00.000000

One level under Topic, same shape/kg_ref pattern as Chapter/Topic. See
Subtopic's model docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a2f8c1e6b93d'
down_revision: Union[str, None] = 'd7e5c2a9f014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'subtopics',
        sa.Column('topic_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('kg_ref', sa.String(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['topic_id'], ['topics.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kg_ref'),
    )


def downgrade() -> None:
    op.drop_table('subtopics')
