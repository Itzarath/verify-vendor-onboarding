"""Record how finance confirmed a bank change (method, where the contact came from, who confirmed).

Earlier decisions have none; they keep counting as before.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE review_resolutions ADD COLUMN confirmation JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE review_resolutions DROP COLUMN confirmation")
