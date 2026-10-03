"""subject per grade

Revision ID: 849faf925d55
Revises: db4d2c0100bc
Create Date: 2026-09-25 08:15:29.358875

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '849faf925d55'
down_revision: Union[str, None] = 'db4d2c0100bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A subject belongs to one grade (see Subject's docstring): names are
    unique per grade, and curriculum kg_refs (CH01, CH01_T02, ...) are
    unique within their parent instead of globally - every grade's KG
    numbers its chapters from CH01."""
    op.add_column('subjects', sa.Column('grade', sa.Integer(), nullable=True))
    # A subject linked to exactly one grade takes it; one linked to several
    # stays NULL (legacy, grade-agnostic) for an admin to split by hand.
    op.execute("""
        UPDATE subjects s SET grade = g.grade
        FROM (SELECT subject_id, min(grade) AS grade FROM subject_grades
              GROUP BY subject_id HAVING count(*) = 1) g
        WHERE g.subject_id = s.id
    """)
    op.drop_constraint('subjects_name_key', 'subjects', type_='unique')
    op.create_unique_constraint('uq_subjects_name_grade', 'subjects', ['name', 'grade'])
    op.drop_constraint('uq_chapters_kg_ref', 'chapters', type_='unique')
    op.create_unique_constraint('uq_chapters_subject_kg_ref', 'chapters', ['subject_id', 'kg_ref'])
    op.drop_constraint('uq_topics_kg_ref', 'topics', type_='unique')
    op.create_unique_constraint('uq_topics_chapter_kg_ref', 'topics', ['chapter_id', 'kg_ref'])
    op.drop_constraint('subtopics_kg_ref_key', 'subtopics', type_='unique')
    op.create_unique_constraint('uq_subtopics_topic_kg_ref', 'subtopics', ['topic_id', 'kg_ref'])


def downgrade() -> None:
    # Fails (by design) if two subjects now share a name or two chapters a kg_ref.
    op.drop_constraint('uq_subtopics_topic_kg_ref', 'subtopics', type_='unique')
    op.create_unique_constraint('subtopics_kg_ref_key', 'subtopics', ['kg_ref'])
    op.drop_constraint('uq_topics_chapter_kg_ref', 'topics', type_='unique')
    op.create_unique_constraint('uq_topics_kg_ref', 'topics', ['kg_ref'])
    op.drop_constraint('uq_chapters_subject_kg_ref', 'chapters', type_='unique')
    op.create_unique_constraint('uq_chapters_kg_ref', 'chapters', ['kg_ref'])
    op.drop_constraint('uq_subjects_name_grade', 'subjects', type_='unique')
    op.create_unique_constraint('subjects_name_key', 'subjects', ['name'])
    op.drop_column('subjects', 'grade')
