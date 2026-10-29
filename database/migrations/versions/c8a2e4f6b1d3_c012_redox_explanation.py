"""C-012 data fix: the written "why" of the bank question nool-sci-ch01-73 (MnO2 + 4HCl ->
MnCl2 + 2H2O + Cl2, "which is oxidised?") said hydrogen goes from +1 to 0 in chlorine
formation. Chlorine goes from -1 to 0. Rows copied from that bank question (practice
pool, MCQ tests) whose explanation has the wrong claim get the right one. Keyed by the
bank question's external_uid; idempotent (a corrected row no longer matches).

Revision ID: c8a2e4f6b1d3
Revises: b4c3e5a7b9d1
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c8a2e4f6b1d3'
down_revision: Union[str, None] = 'b4c3e5a7b9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EXTERNAL_UID = "nool-sci-ch01-73"
RIGHT = ("HCl is oxidised: chlorine goes from -1 in HCl to 0 in Cl₂, so HCl loses electrons "
         "(in school terms, it loses hydrogen). MnO₂ is reduced: manganese goes from +4 to +2.")
WRONG = r"hydrogen\s+(goes|changes|moves)\s+from\s+\+?1\s+to\s+0"


FIX = f"""
    UPDATE {{table}} t SET explanation = '{RIGHT}'
    FROM dataset_questions d
    WHERE t.source_question_id = d.id AND d.external_uid = '{EXTERNAL_UID}'
      AND t.explanation ~* '{WRONG}'
"""


def upgrade() -> None:
    for table in ("practice_questions", "mcq_test_questions"):
        op.execute(FIX.format(table=table))


def downgrade() -> None:
    pass  # the old text was wrong; nothing to restore
