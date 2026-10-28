"""C-009 data fix: three paper MCQ keys were wrong (recomputed by hand).

- ccbae4 Q1: pH 2 + pH 13 mix to about pH 12.7; pH 6 + pH 8 give 7 -> (C).
- ccbae4 Q4: only R (pH 11) holds an alkali -> (C).
- 460596 Q6: A and R are both true, but R (H = I^2 R at fixed R) doesn't explain A (P = V^2/R) -> (b).

Each row changes only while it still holds the wrong answer, so this is a
no-op where the rows don't exist or a teacher has already corrected them.

Revision ID: a9c1e3f5b7d9
Revises: f1b3d5e7a9c2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a9c1e3f5b7d9'
down_revision: Union[str, None] = 'f1b3d5e7a9c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (question id, wrong answer, right answer)
FIXES = [
    ('ade9144e-faf1-4c09-ad82-cd0c0e5d7624', '(B) pH 2 and pH 13', '(C) pH 6 and pH 8'),
    ('3183aca5-9d8c-4600-9aa5-7107a18fe4ad', '(B) Only Q', '(C) Only R'),
    ('4d414ddb-f337-4fa7-bdbc-bd29a1eea11a', '(d) Assertion (A) is false, but Reason (R) is true.',
     '(b) Both Assertion (A) and Reason (R) are true, but R is not the correct explanation of A.'),
]


def _apply(pairs) -> None:
    for qid, old, new in pairs:
        params = {'id': qid, 'old': old, 'new': new}
        op.get_bind().execute(sa.text(
            'UPDATE question_paper_questions SET answer = :new WHERE id = CAST(:id AS uuid) AND answer = :old'), params)
        # a generated answer key starts with the option text; regenerate it from the corrected question
        op.get_bind().execute(sa.text(
            'DELETE FROM question_paper_answer_keys WHERE question_id = CAST(:id AS uuid) AND answer LIKE :old_prefix'),
            {'id': qid, 'old_prefix': old[:3] + '%'})


def upgrade() -> None:
    _apply(FIXES)


def downgrade() -> None:
    # Irreversible by design: the old answers were wrong.
    pass
