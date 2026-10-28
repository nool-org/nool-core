"""C-006 data fix: MCQ re-check rows written before the shuffle kept their
explanation's letters. "Option (A) is correct" named a wrong option once the
options were shuffled; options carried their own "(C) ..." labels; a stem
repeated its options. Also one stem called river water "acid rain".

- mcq_test_questions written by generation (no source question): the shuffle
  is replayed (Random(f"{mcq_test_id}:{order - 1}"), as mcq_test._generate
  seeded it) to move the letters; if the result still doesn't name the key,
  the explanation falls back to "The answer is ...".
- practice_questions written by generation: the shuffle seed isn't stored,
  so an explanation naming a wrong option is cleared (daily_practice._explain
  writes a new one the next time it's picked).
- Options that all carry an "(A)"/"A." label lose it; stem lines that repeat
  the options go.
Idempotent: a fixed row names its key and has no labels, so it is skipped.

Revision ID: b4a1c3e5d7f9
Revises: f1b3d5e7a9c2
"""
import json
import random
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b4a1c3e5d7f9'
down_revision: Union[str, None] = 'f1b3d5e7a9c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# the same patterns as services/core/src/services/mcq_test.py (a migration can't import app code)
_LABEL = re.compile(r"^\s*(?:\([a-dA-D]\)|[a-dA-D][.)])\s+")
_OPTION_LETTERS = re.compile(
    r"\b(?i:options?)\s*(?:\([A-Da-d]\)|[A-D])(?:\s*(?:,|and|or|&)\s*(?:\([A-Da-d]\)|[A-D]))*(?!\w)")
_LETTER = re.compile(r"(?<![A-Za-z])[A-Da-d](?![A-Za-z])")
_CLAIM = re.compile(r"\b(?i:option)\s*\(?([A-Da-d])\)?\s+(?:(?i:is)\s+(?:(?i:the)\s+)?)?(?i:correct|right)")

RIVER = ("the local river water has reached a pH of 5.2 after heavy rains near an industrial area. "
         "This water is now classified as:")
RAIN = "the rainwater collected near an industrial area has a pH of 5.2. This rain is classified as:"


def _unlabel(option: str) -> str:
    return _LABEL.sub("", option).strip() or option.strip()


def _relabel(text: str, order: list[int]) -> str:
    def letter(m):
        abc = "ABCD" if m.group().isupper() else "abcd"
        return abc[order.index(abc.index(m.group()))]
    return _OPTION_LETTERS.sub(lambda m: _LETTER.sub(letter, m.group()), text)


def _claim(text: str | None) -> int | None:
    m = _CLAIM.search(text or "")
    return "abcd".index(m.group(1).lower()) if m else None


def _strip_stem(text: str, options: list[str]) -> str:
    opts = {_unlabel(o).lower() for o in options}
    lines = text.rstrip().split("\n")
    while lines and (not lines[-1].strip() or _unlabel(lines[-1]).lower() in opts) and (
            not lines[-1].strip() or _LABEL.match(lines[-1])):
        lines.pop()
    return "\n".join(lines).rstrip() or text


def _fixed(row, replay_seed: str | None) -> dict:
    options, key, text, why = list(row.options or []), row.correct_index, row.text, row.explanation
    out = {}
    if len(options) == 4 and all(_LABEL.match(o) for o in options):
        options = [_unlabel(o) for o in options]
        if len(set(options)) == 4:
            out["options"] = json.dumps(options)
        else:
            options = list(row.options)
    if len(options) == 4:
        stem = _strip_stem(text, options)
        if stem != text:
            out["text"] = stem
    claim = _claim(why)
    if key is not None and claim is not None and claim != key:
        if replay_seed is None:
            out["explanation"] = None
        else:
            order = list(range(4))
            random.Random(replay_seed).shuffle(order)
            moved = _relabel(why, order)
            out["explanation"] = moved if _claim(moved) == key else f"The answer is {options[key]}."
    return out


def upgrade() -> None:
    bind = op.get_bind()
    for row in bind.execute(sa.text(
            'SELECT id, mcq_test_id, "order", options, correct_index, text, explanation, source_question_id '
            'FROM mcq_test_questions')).all():
        seed = f"{row.mcq_test_id}:{row.order - 1}" if row.source_question_id is None else None
        changes = _fixed(row, seed)
        if row.source_question_id is not None:
            changes.pop("explanation", None)  # bank rows were never shuffled; _explain_bank wrote them in order
        if changes:
            sets = ", ".join(f"{k} = CAST(:{k} AS jsonb)" if k == "options" else f"{k} = :{k}" for k in changes)
            bind.execute(sa.text(f"UPDATE mcq_test_questions SET {sets} WHERE id = :id"), {**changes, "id": row.id})
    for row in bind.execute(sa.text(
            "SELECT id, options, correct_index, text, explanation FROM practice_questions "
            "WHERE kind = 'mcq' AND origin = 'generated'")).all():
        changes = _fixed(row, None)
        if changes:
            sets = ", ".join(f"{k} = CAST(:{k} AS jsonb)" if k == "options" else f"{k} = :{k}" for k in changes)
            bind.execute(sa.text(f"UPDATE practice_questions SET {sets} WHERE id = :id"), {**changes, "id": row.id})


    for table in ("dataset_questions", "practice_questions", "mcq_test_questions", "homework_questions"):
        bind.execute(sa.text(f"UPDATE {table} SET text = replace(text, :river, :rain) WHERE text LIKE :like"),
                     {"river": RIVER, "rain": RAIN, "like": f"%{RIVER}%"})


def downgrade() -> None:
    pass  # a content fix: the old letters were wrong
