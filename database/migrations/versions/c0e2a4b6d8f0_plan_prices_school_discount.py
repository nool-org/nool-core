"""Plan prices ₹1,000 / ₹1,600 / ₹2,200 and subscriptions.discount_percent
(a per-school discount on the plan price, 0-100).

Revision ID: c0e2a4b6d8f0
Revises: b8d0f2a4c6e8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c0e2a4b6d8f0'
down_revision: Union[str, None] = 'b8d0f2a4c6e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRICES = {"Core": ("₹700", "₹1,000"), "Plus": ("₹1,100", "₹1,600"), "Pro": ("₹2,200", "₹2,200")}
UNIT = " / student / subject / year"


def upgrade() -> None:
    op.add_column('subscriptions', sa.Column('discount_percent', sa.Integer(), nullable=False, server_default='0'))
    op.create_check_constraint('ck_subscriptions_discount_percent', 'subscriptions',
                               'discount_percent between 0 and 100')
    for name, (_, new) in PRICES.items():
        op.execute(sa.text("update plans set price_label = :label where name = :name")
                   .bindparams(label=new + UNIT, name=name))


def downgrade() -> None:
    for name, (old, _) in PRICES.items():
        op.execute(sa.text("update plans set price_label = :label where name = :name")
                   .bindparams(label=old + UNIT, name=name))
    op.drop_constraint('ck_subscriptions_discount_percent', 'subscriptions', type_='check')
    op.drop_column('subscriptions', 'discount_percent')
