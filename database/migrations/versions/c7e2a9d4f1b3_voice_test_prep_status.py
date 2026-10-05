"""voice_tests.prep_status - sessions wait while the test's questions are prepared

Revision ID: c7e2a9d4f1b3
Revises: b3d9e1f04a7c
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c7e2a9d4f1b3'
down_revision: Union[str, None] = 'b3d9e1f04a7c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_prep_status = postgresql.ENUM('PENDING', 'READY', 'FAILED', name='voice_test_prep_status')


def upgrade() -> None:
    _prep_status.create(op.get_bind(), checkfirst=True)
    # existing tests are done preparing (with or without questions)
    op.add_column('voice_tests', sa.Column(
        'prep_status', postgresql.ENUM(name='voice_test_prep_status', create_type=False),
        server_default='READY', nullable=False))


def downgrade() -> None:
    op.drop_column('voice_tests', 'prep_status')
    _prep_status.drop(op.get_bind(), checkfirst=True)
