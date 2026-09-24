"""subject kg_subject_name

Revision ID: d7e5c2a9f014
Revises: c4a1f0d6b8e2
Create Date: 2026-09-23 00:00:00.000000

Lets a Subject's nool-kg graph name differ from its catalog display name -
see Subject.kg_subject_name's docstring.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd7e5c2a9f014'
down_revision: Union[str, None] = 'c4a1f0d6b8e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subjects', sa.Column('kg_subject_name', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('subjects', 'kg_subject_name')
