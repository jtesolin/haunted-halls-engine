"""Durable usage ledger, logical request counter and in-flight leases.

Adds ``model_requests.status`` (reserved/completed), the ``usage_requests``
logical-request ledger (backfilled from existing user turns), the seeded
``usage_budget_lock`` row, campaign turn leases and idempotency claim leases.
All new columns are nullable or defaulted, so instances running the previous
image keep working during a rolling deploy (they simply do not write
``usage_requests``, so request caps under-count until the rollout completes).

Downgrade drops the added tables/columns; the request ledger is lost and the
previous turn-based counting resumes. The backfill counts only existing user
turns, so campaign creations and failed requests made before the migration are
absent from that day's request totals.

Revision ID: 0006_durable_usage_leases
Revises: 0005_backfill_characters
"""

import sqlalchemy as sa
from alembic import op


revision = "0006_durable_usage_leases"
down_revision = "0005_backfill_characters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_requests",
        sa.Column("status", sa.String(), nullable=False, server_default="completed"),
    )
    op.create_index("ix_model_requests_created", "model_requests", ["created_at"])
    op.create_index(
        "ix_model_requests_owner_created", "model_requests", ["owner_user_id", "created_at"]
    )
    op.add_column("campaigns", sa.Column("turn_lease_token", sa.String(), nullable=True))
    op.add_column("campaigns", sa.Column("turn_lease_expires_at", sa.Text(), nullable=True))
    op.add_column("chat_request_idempotency", sa.Column("owner_token", sa.String(), nullable=True))
    op.add_column("chat_request_idempotency", sa.Column("lease_expires_at", sa.Text(), nullable=True))

    op.create_table(
        "usage_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["internal_users.user_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usage_requests_owner_created", "usage_requests", ["owner_user_id", "created_at"]
    )
    op.create_index("ix_usage_requests_created", "usage_requests", ["created_at"])

    budget_lock = op.create_table(
        "usage_budget_lock",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("n", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(budget_lock, [{"id": 1, "n": 0}])

    # Preserve current-day request semantics: one logical request per stored user turn.
    op.execute(
        """
        INSERT INTO usage_requests (owner_user_id, kind, created_at)
        SELECT c.owner_user_id, 'chat', t.created_at
        FROM turns t JOIN campaigns c ON c.campaign_id = t.campaign_id
        WHERE t.role = 'user' AND c.owner_user_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_model_requests_owner_created", table_name="model_requests")
    op.drop_index("ix_model_requests_created", table_name="model_requests")
    op.drop_table("usage_budget_lock")
    op.drop_index("ix_usage_requests_created", table_name="usage_requests")
    op.drop_index("ix_usage_requests_owner_created", table_name="usage_requests")
    op.drop_table("usage_requests")
    with op.batch_alter_table("chat_request_idempotency") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("owner_token")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("turn_lease_expires_at")
        batch.drop_column("turn_lease_token")
    with op.batch_alter_table("model_requests") as batch:
        batch.drop_column("status")
