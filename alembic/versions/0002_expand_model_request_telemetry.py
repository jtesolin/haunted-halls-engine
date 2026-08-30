"""Expand model request telemetry to capture full OpenAI Responses API usage metadata.

Revision ID: 0002_expand_model_request_telemetry
Revises: 0001_initial_schema
Create Date: 2026-08-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0002_expand_model_request_telemetry'
down_revision = '0001_initial_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite doesn't support ALTER COLUMN to change nullability, so use batch mode
    with op.batch_alter_table('model_requests', schema=None) as batch_op:
        # Make actual token columns nullable to distinguish between "not reported" and "0"
        batch_op.alter_column('actual_input_tokens',
                   existing_type=sa.Integer(),
                   nullable=True,
                   existing_nullable=False)
        batch_op.alter_column('actual_output_tokens',
                   existing_type=sa.Integer(),
                   nullable=True,
                   existing_nullable=False)
        
        # Add new columns for expanded OpenAI usage metadata
        batch_op.add_column(sa.Column('cached_input_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('cache_write_input_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reasoning_output_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('actual_total_tokens', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('model_requests', schema=None) as batch_op:
        # Remove new columns
        batch_op.drop_column('actual_total_tokens')
        batch_op.drop_column('reasoning_output_tokens')
        batch_op.drop_column('cache_write_input_tokens')
        batch_op.drop_column('cached_input_tokens')
        
        # Revert actual token columns to non-nullable
        batch_op.alter_column('actual_output_tokens',
                   existing_type=sa.Integer(),
                   nullable=False,
                   existing_nullable=True)
        batch_op.alter_column('actual_input_tokens',
                   existing_type=sa.Integer(),
                   nullable=False,
                   existing_nullable=True)

