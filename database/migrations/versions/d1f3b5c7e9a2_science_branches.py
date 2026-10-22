"""Science taught by up to 3 teachers: chapters.branch (PHYSICS / CHEMISTRY /
BIOLOGY) and teacher_class_assignments.branch (NULL = every chapter). The
Class 10 Science chapters are tagged here.

Revision ID: d1f3b5c7e9a2
Revises: c0e2a4b6d8f0
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd1f3b5c7e9a2'
down_revision: Union[str, None] = 'c0e2a4b6d8f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BRANCHES = ('PHYSICS', 'CHEMISTRY', 'BIOLOGY')
CHAPTERS = {
    'CHEMISTRY': ['Chemical Reactions and Equations', 'Acids, Bases and Salts', 'Metals and Non-metals',
                  'Carbon and its Compounds'],
    'BIOLOGY': ['Life Processes', 'Control and Coordination', 'How do Organisms Reproduce?', 'Heredity',
                'Our Environment'],
    'PHYSICS': ['Light – Reflection and Refraction', 'The Human Eye and the Colourful World', 'Electricity',
                'Magnetic Effects of Electric Current'],
}


def upgrade() -> None:
    check = "branch in ('PHYSICS', 'CHEMISTRY', 'BIOLOGY')"
    for table in ('chapters', 'teacher_class_assignments'):
        op.add_column(table, sa.Column('branch', sa.String(), nullable=True))
        op.create_check_constraint(f'ck_{table}_branch', table, check)
    for branch, names in CHAPTERS.items():
        op.execute(sa.text(
            "update chapters set branch = :branch where name = any(:names)"
            " and subject_id in (select id from subjects where name = 'Science')"
        ).bindparams(branch=branch, names=names))


def downgrade() -> None:
    for table in ('chapters', 'teacher_class_assignments'):
        op.drop_constraint(f'ck_{table}_branch', table)
        op.drop_column(table, 'branch')
