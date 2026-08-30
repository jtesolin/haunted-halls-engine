"""Persist configured model output estimates for conservative usage accounting.

Revision ID: 0003_add_estimated_output_tokens
Revises: 0002_expand_model_telemetry
Create Date: 2026-08-30 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0003_add_estimated_output_tokens"
down_revision = "0002_expand_model_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "estimated_output_tokens",
                sa.Integer(),
                nullable=False,
                server_default="500",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("model_requests", schema=None) as batch_op:
        batch_op.drop_column("estimated_output_tokens")