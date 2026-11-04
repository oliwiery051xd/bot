"""Enable real catalog orders and encrypted shipping details."""
from alembic import op
import sqlalchemy as sa

revision = "3f61a0c2d7b9"
down_revision = "37be1f6ad820"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("is_example", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("requires_shipping", sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("shipping_required", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("delivery_ciphertext", sa.Text(), nullable=True))
        batch.add_column(sa.Column("delivery_submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("fulfillment_due_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("orders") as batch:
        for name in ("fulfillment_due_at", "delivery_submitted_at", "delivery_ciphertext", "shipping_required"):
            batch.drop_column(name)
    with op.batch_alter_table("products") as batch:
        batch.drop_column("requires_shipping")
        batch.drop_column("is_example")
