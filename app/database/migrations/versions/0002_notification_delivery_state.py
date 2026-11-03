"""notification delivery state

Revision ID: 0002_delivery_state
Revises: 0001_initial
Create Date: 2026-11-03 20:13:37.321738
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.database.base


revision: str = '0002_delivery_state'
down_revision: str | None = '0001_initial'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add as nullable, backfill, then enforce NOT NULL, so existing databases
    # with rows migrate cleanly. Pre-existing rows were written by the old
    # at-most-once logic only after a claim, so they are recorded as 'sent'.
    with op.batch_alter_table("notification_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("attempts", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("updated_at", app.database.base.UTCDateTime(timezone=True), nullable=True))

    op.execute("UPDATE notification_log SET status = 'sent', attempts = 1, updated_at = created_at")

    with op.batch_alter_table("notification_log", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=32),
            type_=sa.Enum("sending", "sent", "retry", "failed", name="deliverystatus", native_enum=False, length=32),
            nullable=False,
        )
        batch_op.alter_column("attempts", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column(
            "updated_at", existing_type=app.database.base.UTCDateTime(timezone=True), nullable=False
        )
        batch_op.create_index("ix_notification_log_status_updated", ["status", "updated_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("notification_log", schema=None) as batch_op:
        batch_op.drop_index("ix_notification_log_status_updated")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("last_error")
        batch_op.drop_column("attempts")
        batch_op.drop_column("status")
