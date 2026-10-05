"""homework answers and boss question flag

Revision ID: e2b7c9a4d1f3
Revises: c7badbe03a03
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b7c9a4d1f3'
down_revision: Union[str, None] = 'c7badbe03a03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('homework_questions',
                  sa.Column('is_boss', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        'homework_answers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('homework_question_id', sa.UUID(), nullable=False),
        sa.Column('student_id', sa.UUID(), nullable=False),
        sa.Column('answer_text', sa.Text(), nullable=True),
        sa.Column('self_rating', sa.String(), nullable=False),
        sa.Column('xp_awarded', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['homework_question_id'], ['homework_questions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('homework_question_id', 'student_id'),
    )


def downgrade() -> None:
    op.drop_table('homework_answers')
    op.drop_column('homework_questions', 'is_boss')
