"""Link reused readings to the original extraction; document bytes are mandatory.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE extractions ADD COLUMN reused_from_id INTEGER REFERENCES extractions (id)")
    op.execute("ALTER TABLE documents ALTER COLUMN content SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE documents ALTER COLUMN content DROP NOT NULL")
    op.execute("ALTER TABLE extractions DROP COLUMN reused_from_id")
