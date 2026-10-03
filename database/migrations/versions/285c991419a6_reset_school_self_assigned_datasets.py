"""reset school self assigned datasets

Revision ID: 285c991419a6
Revises: 849faf925d55
Create Date: 2026-09-25 14:36:42.719937

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '285c991419a6'
down_revision: Union[str, None] = '849faf925d55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Catalog datasets are now assigned only by a Super Admin. Before that,
    a School Admin could switch them on for their own school (the removed
    PUT /school/datasets), and those rows would still count as assigned.
    school_datasets doesn't record who wrote a row, but the audit log
    records who last changed each school's set ("datasets.updated"): where
    that was not a Super Admin, switch the school's rows off so a Super
    Admin re-assigns deliberately. Schools with no such history (seeded /
    scripted rows) are left alone."""
    op.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (a.target_id) a.target_id, u.role
            FROM audit_logs a JOIN users u ON u.id = a.actor_id
            WHERE a.action = 'datasets.updated' AND a.target_type = 'school'
            ORDER BY a.target_id, a.created_at DESC
        )
        UPDATE school_datasets sd SET enabled = false
        FROM latest
        WHERE sd.school_id::text = latest.target_id AND latest.role <> 'SUPER_ADMIN' AND sd.enabled
    """)


def downgrade() -> None:
    # Irreversible by design: which rows the school itself had enabled isn't recoverable.
    pass
