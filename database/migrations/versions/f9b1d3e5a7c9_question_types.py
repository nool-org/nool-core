"""New question types for the shared practice pool - who_is_right,
make_your_own, fix_steps - each with its type-specific spec, and the
student's answer to one (practice_questions.spec, daily_practice_items.response).
See services/core/src/services/question_types.py.

Revision ID: f9b1d3e5a7c9
Revises: e8a0c2d4f6b8
Create Date: 2026-10-13 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f9b1d3e5a7c9'
down_revision: Union[str, None] = 'e8a0c2d4f6b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('practice_questions', 'daily_practice_items'):  # "make_your_own" doesn't fit in 10
        op.alter_column(table, 'kind', type_=sa.String(20), existing_type=sa.String(10))
    op.add_column('practice_questions', sa.Column('spec', postgresql.JSONB(), nullable=True))
    op.add_column('daily_practice_items', sa.Column('response', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM daily_practice_items WHERE kind IN ('who_is_right', 'make_your_own', 'fix_steps')")
    op.execute("DELETE FROM practice_questions WHERE kind IN ('who_is_right', 'make_your_own', 'fix_steps')")
    op.drop_column('daily_practice_items', 'response')
    op.drop_column('practice_questions', 'spec')
    for table in ('practice_questions', 'daily_practice_items'):
        op.alter_column(table, 'kind', type_=sa.String(10), existing_type=sa.String(20))
