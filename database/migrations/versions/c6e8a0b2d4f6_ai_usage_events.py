"""AI usage events: the measured cost of every generation/colearner AI call
(and each live voice test), for the Super Admin's AI usage view

Revision ID: c6e8a0b2d4f6
Revises: b5d7f9a1c3e5
Create Date: 2026-10-12 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c6e8a0b2d4f6'
down_revision: Union[str, None] = 'b5d7f9a1c3e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No FKs on the school/teacher/student ids: deleting a user or school
    # must never lose its cost history.
    op.create_table(
        'ai_usage_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('service', sa.String(20), nullable=False),  # generation | colearner
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('feature', sa.String(50), nullable=False),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('models', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('calls', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cached_input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('usd', sa.Numeric(12, 6), nullable=False, server_default='0'),
        sa.Column('inr', sa.Numeric(12, 4), nullable=False, server_default='0'),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('status', sa.Integer(), nullable=False),  # HTTP status, 0 = network error
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('school_id', sa.UUID(), nullable=True),
        sa.Column('teacher_id', sa.UUID(), nullable=True),
        sa.Column('student_id', sa.UUID(), nullable=True),
        sa.Column('request_id', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_usage_events_created_at', 'ai_usage_events', ['created_at'])
    op.create_index('ix_ai_usage_events_school_created', 'ai_usage_events', ['school_id', 'created_at'])
    op.create_index('ix_ai_usage_events_feature_created', 'ai_usage_events', ['feature', 'created_at'])
    op.create_index('ix_ai_usage_events_student_id', 'ai_usage_events', ['student_id'])


def downgrade() -> None:
    op.drop_table('ai_usage_events')
