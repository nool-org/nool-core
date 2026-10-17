"""voice_tests.fix_cards and voice_test_questions.card - Fix Milo cards inside
Speak with Milo: Milo puts a worked solution with one slip on screen, the
student taps the slip and the fix, then says why.

Revision ID: f6b8d0a2c4e6
Revises: e5a7c9b1d3f5
Create Date: 2026-10-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f6b8d0a2c4e6'
down_revision: Union[str, None] = 'e5a7c9b1d3f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("voice_tests", sa.Column("fix_cards", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("voice_test_questions", sa.Column("card", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("voice_test_questions", "card")
    op.drop_column("voice_tests", "fix_cards")
