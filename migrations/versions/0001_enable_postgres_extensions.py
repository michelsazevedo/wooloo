"""enable_postgres_extensions

Enable the PostgreSQL extensions the registry domain depends on:

- ``uuid-ossp``: server-side UUID generation for primary keys.
- ``citext``: case-insensitive text columns (repository/namespace names).

Revision ID: 0001_enable_postgres_extensions
Revises:
Create Date: 2026-08-23 15:26:14.547453

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_enable_postgres_extensions"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable the uuid-ossp and citext extensions."""
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "citext"')


def downgrade() -> None:
    """Drop the extensions in reverse order of creation."""
    op.execute('DROP EXTENSION IF EXISTS "citext"')
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')
