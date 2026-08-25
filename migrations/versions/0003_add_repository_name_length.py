"""add_repository_name_length

Bound ``repositories.name`` to 255 characters at the table level.

The application already refuses a longer name: ``RepositoryName`` measures the
stripped value before it builds anything, so an oversized name never reaches a
statement. This constraint is the layer below that, and exists for the writers
the value object does not sit in front of — a future bulk import, a fix-up script,
a second service reaching the same database.

Without it the column's own limits decide, and they decide badly. ``CITEXT``
imposes no length of its own, so a name of any size stores successfully, while
``ix_repositories_name`` rejects entries past PostgreSQL's btree maximum — a
threshold that moves with how compressible the value is, since TOAST compresses
before the index measures. The observable behaviour is therefore a multi-megabyte
name that inserts, or one that fails with ``54000`` at a size nobody can predict.
255 replaces both with a single stated rule.

``length(name::text)`` casts because ``length()`` has no ``citext`` overload; the
cast is to the underlying text and changes nothing about the value.

Revision ID: 0003_add_repository_name_length
Revises: 0002_create_repositories_table
Create Date: 2026-08-24 18:12:07.410882

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_add_repository_name_length"
down_revision: str | Sequence[str] | None = "0002_create_repositories_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "ck_repositories_name_length"
"""
Name of the check constraint, spelled out in full.

It is written literally rather than as the bare ``name_length`` that
``models.base.NAMING_CONVENTION`` would expand, because this environment gives
Alembic no ``target_metadata`` and therefore no convention to expand it with — the
string reaches PostgreSQL exactly as written. The spelling is the one the ``ck``
convention would have produced, so a model that later declares the same
``CheckConstraint`` as ``name="name_length"`` names the identical object instead
of a duplicate under a second name.

"""

_MAX_NAME_LENGTH = 255


def upgrade() -> None:
    """Add the name-length check constraint."""
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        "repositories",
        f"length(name::text) <= {_MAX_NAME_LENGTH}",
    )


def downgrade() -> None:
    """Drop the name-length check constraint."""
    op.drop_constraint(_CONSTRAINT_NAME, "repositories", type_="check")
