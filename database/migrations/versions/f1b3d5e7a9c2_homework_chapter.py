"""Homework a teacher sets straight from a chapter (no Speak with Milo before
it): homework.chapter_id. NULL for homework that follows a voice test
(test_id) or a topic checkpoint (topic_id).

Revision ID: f1b3d5e7a9c2
Revises: e2a4c6d8f0b1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'f1b3d5e7a9c2'
down_revision: Union[str, None] = 'e2a4c6d8f0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('homework', sa.Column('chapter_id', UUID(as_uuid=True), sa.ForeignKey('chapters.id'), nullable=True))


def downgrade() -> None:
    op.drop_column('homework', 'chapter_id')
