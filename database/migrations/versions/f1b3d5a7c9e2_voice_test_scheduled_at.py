"""voice_tests.scheduled_at - the start of a test's completion window

Revision ID: f1b3d5a7c9e2
Revises: e8a1c3f5b7d9
Create Date: 2026-10-10 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1b3d5a7c9e2'
down_revision: Union[str, None] = 'e8a1c3f5b7d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('voice_tests', sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True))
    # already-published tests: creation time is the closest thing to their schedule time
    op.execute("UPDATE voice_tests SET scheduled_at = created_at WHERE status <> 'DRAFT'")


def downgrade() -> None:
    op.drop_column('voice_tests', 'scheduled_at')
