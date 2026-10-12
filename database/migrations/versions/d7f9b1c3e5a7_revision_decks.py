"""Revision decks: one set of revision cards per topic, generated once and
shown to every student

Revision ID: d7f9b1c3e5a7
Revises: c6e8a0b2d4f6
Create Date: 2026-10-12 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd7f9b1c3e5a7'
down_revision: Union[str, None] = 'c6e8a0b2d4f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'revision_decks',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('topic_id', sa.UUID(), sa.ForeignKey('topics.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chapter_id', sa.UUID(), sa.ForeignKey('chapters.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),  # ready | generating | failed
        sa.Column('cards', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('source', sa.String(20), nullable=False, server_default='generated'),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('generated_by', sa.UUID(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('topic_id', name='uq_revision_decks_topic_id'),
    )
    op.create_index('ix_revision_decks_chapter_id', 'revision_decks', ['chapter_id'])


def downgrade() -> None:
    op.drop_table('revision_decks')
