"""Store each revision's canonical bank-account key, so other requests are matched on it instead of on SQL cleaning.

Existing revisions are filled in with the application's own key function (crossfacts.bank_key), the one every later
revision is written with.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE revisions ADD COLUMN bank_account_key TEXT")
    op.execute("CREATE INDEX ix_revisions_bank_account_key ON revisions (bank_account_key)")
    backfill(op.get_bind())


def backfill(conn) -> int:
    """Fill in the key of every revision that has none (tests call this directly)."""
    from app.pipeline.crossfacts import bank_key

    n = 0
    for rid, form in conn.execute(sa.text("SELECT id, form FROM revisions WHERE bank_account_key IS NULL")).all():
        key = bank_key(form)
        if key:
            conn.execute(sa.text("UPDATE revisions SET bank_account_key = :k WHERE id = :id"), {"k": key, "id": rid})
            n += 1
    return n


def downgrade() -> None:
    op.execute("DROP INDEX ix_revisions_bank_account_key")
    op.execute("ALTER TABLE revisions DROP COLUMN bank_account_key")
