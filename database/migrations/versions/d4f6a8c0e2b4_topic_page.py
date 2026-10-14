"""topics.page - the textbook page a topic starts on, for "read page N"

Revision ID: d4f6a8c0e2b4
Revises: c3e5a7b9d1f3
Create Date: 2026-10-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd4f6a8c0e2b4'
down_revision: Union[str, None] = 'c3e5a7b9d1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("topics", sa.Column("page", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("topics", "page")
