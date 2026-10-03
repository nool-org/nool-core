"""school owned datasets

Revision ID: db4d2c0100bc
Revises: f8e6908ecfff
Create Date: 2026-09-25 06:33:20.517367

datasets.school_id: NULL = platform catalog (assigned to schools by a
Super Admin), set = that school's own question bank. See Dataset's model
docstring.

Data: every custom_questions row moves into a school-owned dataset - one
dataset per (school, collection, subject, grade), "General bank" for
questions with no collection. Section-level questions (class_id) take
that section's grade: school banks are per grade. custom_questions itself
is left in place (unused) until the move has been checked.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'db4d2c0100bc'
down_revision: Union[str, None] = 'f8e6908ecfff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('datasets', sa.Column('school_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_datasets_school_id'), 'datasets', ['school_id'], unique=False)
    op.create_foreign_key('fk_datasets_school_id', 'datasets', 'schools', ['school_id'], ['id'])

    # One school-owned dataset per (school, collection, subject, grade).
    op.execute("""
        CREATE TEMP TABLE _cq AS
        SELECT cq.*, COALESCE(cq.grade, c.grade) AS resolved_grade,
               COALESCE(NULLIF(TRIM(cq.collection_name), ''), 'General bank') AS bank_name
        FROM custom_questions cq LEFT JOIN classes c ON c.id = cq.class_id
    """)
    op.execute("""
        INSERT INTO datasets (id, name, question_count, description, restricted, type, subject_id, board, grade, school_id)
        SELECT gen_random_uuid(), g.bank_name, g.n, '', false, 'QA', g.subject_id, s.board, g.resolved_grade, g.school_id
        FROM (SELECT school_id, bank_name, subject_id, resolved_grade, count(*) AS n
              FROM _cq GROUP BY school_id, bank_name, subject_id, resolved_grade) g
        JOIN schools s ON s.id = g.school_id
    """)
    op.execute("""
        INSERT INTO dataset_questions (id, created_at, dataset_id, chapter_id, topic_id, bloom_level, question_type,
                                       text, options, answer, created_by, external_uid, source)
        SELECT gen_random_uuid(), q.created_at, d.id, q.chapter_id, q.topic_id, q.bloom_level, q.question_type,
               q.text, q.options, q.answer, q.created_by, 'custom:' || q.id,
               jsonb_build_object('migrated_from', 'custom_questions', 'custom_question_id', q.id)
        FROM _cq q JOIN datasets d ON d.school_id = q.school_id AND d.name = q.bank_name
             AND d.subject_id = q.subject_id AND d.grade IS NOT DISTINCT FROM q.resolved_grade
    """)
    op.execute("DROP TABLE _cq")


def downgrade() -> None:
    # Only the rows copied from custom_questions (still there) are recoverable;
    # questions schools wrote into their banks since would be lost - refuse.
    written_since = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM dataset_questions q JOIN datasets d ON d.id = q.dataset_id "
        "WHERE d.school_id IS NOT NULL AND q.source->>'migrated_from' IS DISTINCT FROM 'custom_questions'"
    )).scalar()
    if written_since:
        raise RuntimeError(f"{written_since} school question-bank questions exist that custom_questions "
                           "doesn't have - export them before downgrading past db4d2c0100bc.")
    op.execute("DELETE FROM dataset_questions WHERE source->>'migrated_from' = 'custom_questions'")
    op.execute("DELETE FROM school_datasets WHERE dataset_id IN (SELECT id FROM datasets WHERE school_id IS NOT NULL)")
    op.execute("DELETE FROM datasets WHERE school_id IS NOT NULL")
    op.drop_constraint('fk_datasets_school_id', 'datasets', type_='foreignkey')
    op.drop_index(op.f('ix_datasets_school_id'), table_name='datasets')
    op.drop_column('datasets', 'school_id')
