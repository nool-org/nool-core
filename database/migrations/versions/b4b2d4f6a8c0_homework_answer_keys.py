"""C-008 data fix: homework answer keys copied from the bank as they were.

- A marker's note left in a key ("Marking scheme indicates (d)/(B),(C) and (D)
  for another question but ...") is cut, sentence by sentence.
- "Ans. (2)" on a line of its own (the source paper's numbering) is cut.
- The brine assertion-reason key that gave two answers ("(d) ..." then "(c) A is
  true but R is false" with sodium at the anode) keeps (d), with the right
  explanation (chlorine at the anode, hydrogen at the cathode).
The bank rows themselves are kept out of new homework by
question_selector.sound_key. Idempotent: a cleaned answer no longer matches.

Revision ID: b4b2d4f6a8c0
Revises: b4a1c3e5d7f9
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b4b2d4f6a8c0'
down_revision: Union[str, None] = 'b4a1c3e5d7f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NOTE = re.compile(r"[^.\n]*(?:marking scheme|another question)[^.\n]*\.?", re.IGNORECASE)
_ANS_NUMBER = re.compile(r"^\s*Ans\.?\s*\(\d+\)\s*$", re.IGNORECASE | re.MULTILINE)
BRINE_WRONG = ("(c) A is true but R is false.\nExplanation: During electrolysis of a concentrated aqueous solution "
               "of sodium chloride. Ions get attracted to oppositely charged electrodes, sodium is produced at the "
               "anode and chlorine gas is produced at the cathode.")
BRINE_RIGHT = ("Explanation: In the electrolysis of a concentrated aqueous solution of sodium chloride (brine), "
               "chlorine gas is given off at the anode and hydrogen gas at the cathode; sodium hydroxide stays "
               "in the solution.")


def clean(answer: str) -> str:
    out = answer.replace(BRINE_WRONG, BRINE_RIGHT) if answer.startswith("(d) A is false but R is true.") else answer
    out = _ANS_NUMBER.sub("", _NOTE.sub("", out))
    out = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", out)).strip()
    return out or answer


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, answer FROM homework_questions WHERE answer ~* 'marking scheme|another question|ans\\.?\\s*\\(\\d+\\)'"
        " OR answer LIKE :brine"), {"brine": f"%{BRINE_WRONG[:40]}%"}).all()
    for row in rows:
        fixed = clean(row.answer)
        if fixed != row.answer:
            bind.execute(sa.text("UPDATE homework_questions SET answer = :answer WHERE id = :id"),
                         {"answer": fixed, "id": row.id})


def downgrade() -> None:
    pass  # a content fix: the old keys were wrong
