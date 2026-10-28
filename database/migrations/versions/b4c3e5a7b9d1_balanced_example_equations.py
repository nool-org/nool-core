"""C-007 data fix: two bank case-study stems print an unbalanced example
equation (in LaTeX) - "2H2 + O2 = H2O" and "2NaOH + H2SO4 -> Na2SO4 + H2O".
They are replaced, balanced and in plain Unicode, wherever the stem was copied
(bank, practice pool, re-check, homework). Idempotent: replace() of text that
is no longer there changes nothing.

Revision ID: b4c3e5a7b9d1
Revises: b4b2d4f6a8c0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b4c3e5a7b9d1'
down_revision: Union[str, None] = 'b4b2d4f6a8c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FIXES = [
    (r"$2H_2 + O_2 = H_2O$", "2H₂ + O₂ → 2H₂O"),
    (r"$2\mathrm{NaOH} + \mathrm{H}_2\mathrm{SO}_4 \rightarrow \mathrm{Na}_2\mathrm{SO}_4 + \mathrm{H}_2\mathrm{O}$",
     "2NaOH + H₂SO₄ → Na₂SO₄ + 2H₂O"),
]
TABLES = ("dataset_questions", "practice_questions", "mcq_test_questions", "homework_questions")


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        for wrong, right in FIXES:
            bind.execute(sa.text(f"UPDATE {table} SET text = replace(text, :wrong, :right) "
                                 "WHERE strpos(text, :wrong) > 0"), {"wrong": wrong, "right": right})


def downgrade() -> None:
    pass  # a content fix: the old equations were wrong
