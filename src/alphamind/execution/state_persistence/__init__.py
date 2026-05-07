"""State-persistence package: per-component primitives that store and read OMS state.

Public surface (story 01): the frozen ``StatePersistenceConfig`` and its
``load_state_persistence_config`` loader. Later stories add SQLAlchemy tables
under ``tables/``, the repository under ``repository/``, the Phase 1/Phase 2
write paths under ``write_paths/``, and the transactional invocation context
under ``invocation_context/``.
"""

from alphamind.execution.state_persistence.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
)

__all__ = [
    "StatePersistenceConfig",
    "load_state_persistence_config",
]
