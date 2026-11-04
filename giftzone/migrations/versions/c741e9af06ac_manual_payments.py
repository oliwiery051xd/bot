"""Add manual payment details without converting legacy demo balances."""
from alembic import op
import sqlalchemy as sa

revision = "c741e9af06ac"
down_revision = "6ccc4f6277e5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("demo_topups") as batch:
        batch.add_column(sa.Column("asset", sa.String(10), nullable=True))
        batch.add_column(sa.Column("network", sa.String(80), nullable=True))
        batch.add_column(sa.Column("wallet_address", sa.String(200), nullable=True))
        batch.add_column(sa.Column("crypto_amount", sa.String(100), nullable=True))
        batch.add_column(sa.Column("quote_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("confirmation_reference", sa.String(180), nullable=True))
        batch.create_unique_constraint("uq_topups_confirmation_reference", ["confirmation_reference"])


def downgrade():
    with op.batch_alter_table("demo_topups") as batch:
        batch.drop_constraint("uq_topups_confirmation_reference", type_="unique")
        for name in ("confirmation_reference", "submitted_at", "quote_expires_at", "crypto_amount", "wallet_address", "network", "asset"):
            batch.drop_column(name)
