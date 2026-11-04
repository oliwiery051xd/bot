"""Persist the exchange-rate evidence used for each automatic crypto quote."""
from alembic import op
import sqlalchemy as sa

revision = "37be1f6ad820"
down_revision = "c741e9af06ac"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("demo_topups") as batch:
        batch.add_column(sa.Column("rate_pln", sa.String(100), nullable=True))
        batch.add_column(sa.Column("rate_source", sa.String(100), nullable=True))
        batch.add_column(sa.Column("rate_source_updated_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rate_fetched_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rate_mode", sa.String(20), nullable=True))


def downgrade():
    with op.batch_alter_table("demo_topups") as batch:
        for name in ("rate_mode", "rate_fetched_at", "rate_source_updated_at", "rate_source", "rate_pln"):
            batch.drop_column(name)
