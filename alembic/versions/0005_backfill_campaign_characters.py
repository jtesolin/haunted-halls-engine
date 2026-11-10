"""Backfill a starter character for campaigns that have none.

Campaigns created before the application began creating characters have no
``characters`` row, so their ``character_id`` is null. This data-only migration
is idempotent: it only inserts for campaigns with no character row, using the
same deterministic ID/name as the application path, and never touches
campaigns that already have one. Inventory and abilities are not copied; the
character-detail API projects them from each campaign's persisted state.

Revision ID: 0005_backfill_characters
Revises: 0004_chat_idempotency
"""

from alembic import op


revision = "0005_backfill_characters"
down_revision = "0004_chat_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO characters (character_id, campaign_id, name, created_at)
        SELECT 'character_' || c.campaign_id, c.campaign_id, 'Investigator', c.created_at
        FROM campaigns c
        WHERE NOT EXISTS (
            SELECT 1 FROM characters ch WHERE ch.campaign_id = c.campaign_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM characters ch WHERE ch.character_id = 'character_' || c.campaign_id
        )
        """
    )


def downgrade() -> None:
    # Backfilled rows are indistinguishable from application-created ones and
    # are valid data; leave them in place.
    pass
