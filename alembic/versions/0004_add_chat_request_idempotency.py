"""Add durable idempotent chat request claims.

Revision ID: 0004_chat_idempotency
Revises: 0003_add_estimated_output_tokens
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_chat_idempotency"
down_revision = "0003_add_estimated_output_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_request_idempotency",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("requested_campaign_id", sa.String(), nullable=True),
        sa.Column("requested_character_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolved_campaign_id", sa.String(), nullable=True),
        sa.Column("turn_id", sa.String(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["internal_users.user_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_chat_request_idempotency_owner_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("chat_request_idempotency")
