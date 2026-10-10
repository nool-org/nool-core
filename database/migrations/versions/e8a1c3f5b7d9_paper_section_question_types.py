"""question_paper_sections.question_types (per-section types, e.g. from a template)

Revision ID: e8a1c3f5b7d9
Revises: d7f2b4a6c8e1
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e8a1c3f5b7d9'
down_revision: Union[str, None] = 'd7f2b4a6c8e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('question_paper_sections',
                  sa.Column('question_types', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('question_paper_sections', 'question_types')
