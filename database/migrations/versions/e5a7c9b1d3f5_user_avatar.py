"""users.avatar - the picture a user picked (a key into the app's 3D set)

Revision ID: e5a7c9b1d3f5
Revises: d4f6a8c0e2b4
Create Date: 2026-10-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e5a7c9b1d3f5'
down_revision: Union[str, None] = 'd4f6a8c0e2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar")
