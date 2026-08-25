"""
Declarative base and metadata shared by every SQLAlchemy model in this package.

"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""
Deterministic names for indexes and constraints the database would otherwise name
itself. Fixing them at the first model means a later ``downgrade()`` can drop a
constraint by name, and a later autogenerate run compares names rather than
proposing to recreate everything.

"""


class Base(DeclarativeBase):
    """Declarative base every persistence model in this package inherits from.

    Holds the single :class:`~sqlalchemy.MetaData` that collects every mapped
    table, so Alembic can be pointed at one registry once models are added.
    Deliberately carries no columns or mixins: shared columns belong to the
    models that actually share them, not to every future table by default.

    Attributes:
        metadata: Registry of all mapped tables, using
            :data:`NAMING_CONVENTION` for generated constraint names.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
