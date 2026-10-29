"""T-011: student_test_results.created_at, for Today's "+N this week". Existing rows
take their test's due date (never later than now), else the test's creation time.

Revision ID: e3c5d7f9a1b2
Revises: d2b4c6e8f0a1
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'e3c5d7f9a1b2'
down_revision: Union[str, Sequence[str], None] = 'd2b4c6e8f0a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE student_test_results ADD COLUMN IF NOT EXISTS created_at timestamptz")
    op.execute("""
        UPDATE student_test_results r SET created_at = LEAST(now(), COALESCE(
            t.scheduled_at + make_interval(hours => t.completion_window_hours), t.created_at))
        FROM voice_tests t WHERE t.id = r.test_id AND r.created_at IS NULL
    """)
    op.execute("UPDATE student_test_results SET created_at = now() WHERE created_at IS NULL")
    op.execute("ALTER TABLE student_test_results ALTER COLUMN created_at SET DEFAULT now(), "
               "ALTER COLUMN created_at SET NOT NULL")


def downgrade() -> None:
    op.drop_column("student_test_results", "created_at")
