"""Cross-cutting state-persistence infrastructure.

Promoted from ``alphamind.execution.state_persistence`` by ALP-470: this
package now owns the shared SQLAlchemy table definitions, the read-side
repository, the transactional invocation context, the per-process
``process_lifetimes`` writer, the frozen ``StatePersistenceConfig``, and
the cross-cutting Pydantic record facades (``FillRecord``,
``FillProcessingStatus``, ``CorporateActionLedgerStatus``,
``RegTMarginAttribution``) consumed across scheduler, monitor, execution,
decision, and verification scripts.

OMS-specific write paths (fill collection, command execution by command kind, fill
persistence, and the corporate-action integration ledger) remain in
``alphamind.execution.write_paths`` — they are
single-consumer to the execution layer.
"""

from alphamind.state.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
)

__all__ = [
    "StatePersistenceConfig",
    "load_state_persistence_config",
]
