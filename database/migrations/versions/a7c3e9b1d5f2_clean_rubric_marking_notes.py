"""Data only: drop the marking note voice prep used to put into expected_points
("Any two or three of these are enough.", alone or at the end of a point) - the
student Insights screen showed it as a missed idea (S-004, C-004). Also replaces
one wrong rubric point ("this oxidation is endothermic"). Idempotent; no downgrade
(the notes were never content).

Revision ID: a7c3e9b1d5f2
Revises: f1b3d5e7a9c2
"""
import json
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7c3e9b1d5f2'
down_revision: Union[str, None] = 'f1b3d5e7a9c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# same pattern as generation's voice_prep._MARKING_NOTE
NOTE = re.compile(r"\s*\bany (?:one|two|three|four|\d+)(?: or (?:two|three|four|\d+))?(?: of (?:these|them))?"
                  r" (?:is|are) enough\b\.?\s*$", re.IGNORECASE)
WRONG = "this oxidation is endothermic"
RIGHT = ("Burning ethanol is combustion and gives carbon dioxide and water; "
         "oxidising agents change it only to ethanoic acid.")


def clean(points):
    if not isinstance(points, list):
        return points
    out = []
    for p in points:
        if isinstance(p, str):
            p = RIGHT if WRONG in p.lower() else NOTE.sub("", p).strip()
            if not p:
                continue
        out.append(p)
    return out


def _fix(table: str, has_easier: bool) -> None:
    bind = op.get_bind()
    cols = "id, expected_points" + (", easier_version" if has_easier else "")
    where = "expected_points::text ILIKE '%enough%' OR expected_points::text ILIKE '%endothermic%'"
    if has_easier:
        where += " OR easier_version::text ILIKE '%enough%'"
    for row in bind.execute(sa.text(f"SELECT {cols} FROM {table} WHERE {where}")).mappings():
        params = {"id": row["id"], "points": json.dumps(clean(row["expected_points"]))}
        sets = "expected_points = CAST(:points AS jsonb)"
        if has_easier and isinstance(row["easier_version"], dict):
            easier = {**row["easier_version"], "expected_points": clean(row["easier_version"].get("expected_points"))}
            params["easier"] = json.dumps(easier)
            sets += ", easier_version = CAST(:easier AS jsonb)"
        bind.execute(sa.text(f"UPDATE {table} SET {sets} WHERE id = :id"), params)


def upgrade() -> None:
    _fix("voice_test_questions", has_easier=True)
    _fix("practice_questions", has_easier=False)


def downgrade() -> None:
    pass
