"""Core / Plus / Pro plans: plans.spoken_days_per_week and
plans.extra_challenges_per_month, and the three plans themselves (priced per
student, per subject, per school year, before GST). Existing plans keep
every feature (enabled_features NULL).

Revision ID: b8d0f2a4c6e8
Revises: a7c9e1b3d5f7
Create Date: 2026-10-16
"""
import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8d0f2a4c6e8'
down_revision: Union[str, None] = 'a7c9e1b3d5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TEACHER_TOOLS = ["question_paper", "voice_test", "homework", "assistant", "leaderboard", "improvement_analysis"]
PLANS = [
    # name, price label, features, spoken days a week, extra challenges a month
    ("Core", "₹700 / student / subject / year", TEACHER_TOOLS, None, 0),
    ("Plus", "₹1,100 / student / subject / year", TEACHER_TOOLS + ["daily_practice", "spoken_answers"], 3, 0),
    ("Pro", "₹2,200 / student / subject / year", TEACHER_TOOLS + ["daily_practice", "spoken_answers"], None, 1),
]


def upgrade() -> None:
    op.add_column('plans', sa.Column('spoken_days_per_week', sa.Integer(), nullable=True))
    op.add_column('plans', sa.Column('extra_challenges_per_month', sa.Integer(), nullable=False, server_default='0'))
    conn = op.get_bind()
    for name, price, features, spoken, extra in PLANS:
        conn.execute(sa.text(
            "insert into plans (id, name, price_label, teacher_limit, student_limit, enabled_features, active, "
            "spoken_days_per_week, extra_challenges_per_month) values (:id, :name, :price, 200, 5000, "
            "cast(:features as jsonb), true, :spoken, :extra) on conflict (name) do nothing"),
            {"id": uuid.uuid4(), "name": name, "price": price, "features": json.dumps(features),
             "spoken": spoken, "extra": extra})


def downgrade() -> None:
    op.execute("delete from plans where name in ('Core', 'Plus', 'Pro') and not exists "
               "(select 1 from subscriptions s where s.plan_id = plans.id)")
    op.drop_column('plans', 'extra_challenges_per_month')
    op.drop_column('plans', 'spoken_days_per_week')
