"""Merge the migration heads c0d1e2f3a4b5 and a9c1e3f5b7d9 (B5). No schema change.

Revision ID: c1e2f3a4b5c6
Revises: c0d1e2f3a4b5, a9c1e3f5b7d9
"""
from typing import Sequence, Union

revision: str = 'c1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = ('c0d1e2f3a4b5', 'a9c1e3f5b7d9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
