"""question_paper_templates: scope (personal/school) + optional subject

Revision ID: d7f2b4a6c8e1
Revises: c5e7a9d1f3b2
Create Date: 2026-10-07 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f2b4a6c8e1'
down_revision: Union[str, None] = 'c5e7a9d1f3b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # rows saved before scopes existed were shared school-wide - keep them so
    op.add_column('question_paper_templates',
                  sa.Column('scope', sa.String(), server_default='school', nullable=False))
    op.alter_column('question_paper_templates', 'scope', server_default='personal')
    op.alter_column('question_paper_templates', 'subject_id', existing_type=sa.UUID(), nullable=True)


def downgrade() -> None:
    # subject-less (designed) templates can't exist without the column change
    op.execute('DELETE FROM question_paper_templates WHERE subject_id IS NULL')
    op.alter_column('question_paper_templates', 'subject_id', existing_type=sa.UUID(), nullable=False)
    op.drop_column('question_paper_templates', 'scope')
