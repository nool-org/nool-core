"""student_test_results.insights_words (durable insight words)

Revision ID: b4d8e2f6a1c9
Revises: e5f1c7a2d9b3
Create Date: 2026-10-05 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b4d8e2f6a1c9'
down_revision: Union[str, None] = 'e5f1c7a2d9b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('student_test_results', sa.Column('insights_words', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('student_test_results', 'insights_words')
