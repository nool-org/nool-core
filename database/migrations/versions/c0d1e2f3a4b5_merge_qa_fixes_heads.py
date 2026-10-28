"""Merge the B2 (a7c3e9b1d5f2) and B4 (b4b2d4f6a8c0) migration heads. No schema change.

Revision ID: c0d1e2f3a4b5
Revises: a7c3e9b1d5f2, b4b2d4f6a8c0
"""
from typing import Sequence, Union

revision: str = 'c0d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = ('a7c3e9b1d5f2', 'b4b2d4f6a8c0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
