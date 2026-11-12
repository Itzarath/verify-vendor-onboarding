"""Keep a reviewer's reasoning internal: a "not acceptable" decision stores the request to the vendor separately.

Earlier decisions have no vendor text; the vendor then sees a neutral request instead of the reviewer's note.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE review_resolutions ADD COLUMN vendor_text TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE review_resolutions DROP COLUMN vendor_text")
