"""Append-only corporate-action integration-ledger helper (story 05 / ALP-363).

Called from fill collection inside the open ``InvocationContext`` transaction to mark
an Alpaca CA activity as integrated. Idempotent on ``alpaca_activity_id`` so
fill collection retry (transient failure during the integration transaction) does not
double-count.

Unlike ``append_fill_record`` (monitor path, owns its own transaction), this
helper takes the ``InvocationHandle`` from the surrounding ``InvocationContext``
because the CA-integration write must commit atomically with the position /
order / cash mutations that the same activity drives.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.records import (
    CorporateActionLedgerStatus,
)
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)


async def mark_ca_activity_processed(
    handle: InvocationHandle,
    alpaca_activity_id: str,
    *,
    processing_timestamp: datetime,
) -> None:
    """Insert a ledger row for *alpaca_activity_id*; no-op if already present."""
    stmt = sqlite_insert(CorporateActionIntegrationLedgerRow).values(
        alpaca_activity_id=alpaca_activity_id,
        processing_invocation_id=handle.invocation_id,
        processing_timestamp=processing_timestamp.isoformat(),
        processing_status=CorporateActionLedgerStatus.PROCESSED.value,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["alpaca_activity_id"])
    await handle.session.execute(stmt)


__all__ = ["mark_ca_activity_processed"]
