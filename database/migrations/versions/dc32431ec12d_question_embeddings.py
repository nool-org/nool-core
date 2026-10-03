"""question embeddings

Revision ID: dc32431ec12d
Revises: 162087dcdc9a
Create Date: 2026-09-24 10:00:37.815479

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dc32431ec12d'
down_revision: Union[str, None] = '162087dcdc9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Needs the pgvector-enabled image (database/postgres/Dockerfile).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('question_embeddings',
    sa.Column('dataset_question_id', sa.UUID(), nullable=False),
    sa.Column('model', sa.String(), nullable=False),
    sa.Column('content_hash', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['dataset_question_id'], ['dataset_questions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('dataset_question_id')
    )
    # text-embedding-3-small is 1536-dim; raw SQL keeps this migration free
    # of app imports (the model's Vector type lives in src/).
    op.execute("ALTER TABLE question_embeddings ADD COLUMN embedding vector(1536) NOT NULL")
    op.execute(
        "CREATE INDEX ix_question_embeddings_embedding_hnsw ON question_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table('question_embeddings')
    # The extension is left installed - harmless, and other objects may use it.
