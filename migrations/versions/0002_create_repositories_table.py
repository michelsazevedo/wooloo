"""create_repositories_table

Create the ``repositories`` table backing the repository registry domain.

Notes on the schema:

- ``id`` is server-generated with ``uuid_generate_v4()`` (``uuid-ossp``, enabled by
  ``0001``) so the database owns identity even for inserts that bypass the ORM.
- ``name`` is ``CITEXT`` (``citext``, also enabled by ``0001``), so uniqueness is
  case-insensitive without a functional index on ``lower(name)``.
- ``ix_repositories_name`` is a UNIQUE index rather than a table-level UNIQUE
  constraint: in PostgreSQL a UNIQUE constraint is *implemented* as a unique index,
  so this gives both the conflict detection the store's insert path relies on and a
  queryable index for ``get_by_name`` — with a single, explicitly named object
  instead of a constraint plus a redundant duplicate index.
- ``ix_repositories_deleted_at`` supports the ``deleted_at IS NULL`` filter that
  every list/get query applies.

Revision ID: 0002_create_repositories_table
Revises: 0001_enable_postgres_extensions
Create Date: 2026-08-24 14:42:54.733321

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_create_repositories_table"
down_revision: str | Sequence[str] | None = "0001_enable_postgres_extensions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the repositories table and its indexes."""
    op.create_table(
        "repositories",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_repositories_name", "repositories", ["name"], unique=True)
    op.create_index("ix_repositories_deleted_at", "repositories", ["deleted_at"])


def downgrade() -> None:
    """Drop the repositories table and its indexes, in reverse order of creation."""
    op.drop_index("ix_repositories_deleted_at", table_name="repositories")
    op.drop_index("ix_repositories_name", table_name="repositories")
    op.drop_table("repositories")
