"""SQLAlchemy table definitions for the state-persistence layer.

Importing this package registers all state-persistence tables on
``alphamind.persistence.models.Base.metadata`` so callers (tests using
``Base.metadata.create_all``, the Alembic env, downstream stories) see
them without having to remember each module name.
"""

from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.tables.process_lifetimes import ProcessLifetimeRow

__all__ = ["InvocationRow", "ProcessLifetimeRow"]
