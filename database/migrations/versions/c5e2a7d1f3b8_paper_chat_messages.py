"""paper_chat_messages (Paper Copilot chat history)

Revision ID: c5e2a7d1f3b8
Revises: c1f5a9e3b7d2
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5e2a7d1f3b8'
down_revision: Union[str, None] = 'c1f5a9e3b7d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'paper_chat_messages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('paper_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('proposals', postgresql.JSONB(), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['paper_id'], ['question_papers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_paper_chat_messages_paper_id', 'paper_chat_messages', ['paper_id'])


def downgrade() -> None:
    op.drop_index('ix_paper_chat_messages_paper_id', table_name='paper_chat_messages')
    op.drop_table('paper_chat_messages')
