"""V-008: drop stored Bloom growth for a level the re-check asked fewer than 2 times
(Class growth then shows it 'Not assessed', not a one-answer swing). Idempotent.

Revision ID: d2b4c6e8f0a1
Revises: c1e2f3a4b5c6
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'd2b4c6e8f0a1'
down_revision: Union[str, Sequence[str], None] = 'c1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

THIN = """(e.g->>'kind' = 'bloom' AND (SELECT count(*) FROM mcq_test_questions q
            WHERE q.mcq_test_id = m.id AND q.bloom_level::text = e.g->>'key') < 2)"""


def upgrade() -> None:
    op.execute(f"""
        UPDATE mcq_tests m SET result = jsonb_set(m.result, '{{growth}}', COALESCE(
            (SELECT jsonb_agg(e.g ORDER BY e.n) FROM jsonb_array_elements(m.result->'growth') WITH ORDINALITY e(g, n)
             WHERE NOT {THIN}), '[]'::jsonb))
        WHERE jsonb_typeof(m.result->'growth') = 'array'
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(m.result->'growth') e(g) WHERE {THIN})
    """)


def downgrade() -> None:
    pass  # the dropped entries were one-question noise; nothing to restore
