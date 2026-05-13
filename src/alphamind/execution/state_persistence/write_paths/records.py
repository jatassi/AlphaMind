"""Typed Pydantic façades for fill events.

Story 05 / ALP-363 introduces an append-only Tier-2 lifecycle record with no
prior counterpart in ``portfolio_state.records``: ``FillRecord`` is the OMS-
internal authoritative source for cost-basis reconstruction and the input to
the Phase 1 fill-integration write path. The ``activity_log`` carries
`order_filled` / `order_partially_filled` events for audit, but the fill
record itself lives here.

The application-level invariant below is stronger than the SQL CHECK can
express: an ``unprocessed`` fill must have both ``processing_invocation_id``
and ``processing_timestamp`` null; a ``processed`` or ``quarantined`` fill
must have both non-null (set by the Phase 1 invocation that handled it).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from alphamind._kernel.money import Money, Price
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.portfolio_state.records.positions import LiveExecutionEstimate


class FillProcessingStatus(StrEnum):
    """Lifecycle of a fill record.

    UNPROCESSED — persisted by the continuous monitor; not yet integrated.
    PROCESSED   — integrated into positions / orders / cash by a Phase 1.
    QUARANTINED — Phase 1 validation rejected the fill; excluded from
                  integration but preserved for reconciliation.
    """

    UNPROCESSED = "unprocessed"
    PROCESSED = "processed"
    QUARANTINED = "quarantined"


class CorporateActionLedgerStatus(StrEnum):
    """Lifecycle of a corporate-action integration ledger entry.

    Only one terminal value: a row exists in this table iff the activity has
    been integrated. Pre-integration state lives in Alpaca's activity feed,
    not locally — see ``corporate-actions.md`` for the integration spec.
    """

    PROCESSED = "processed"


class RegTMarginAttribution(BaseModel):
    """Per-fill Reg T margin attribution sub-record.

    Mirrors ``regt-margin-attribution.md § Outputs``. ``regt_excess_over_pm``
    is the headline value: the dollar cost of running on Reg T attributable
    to this fill. Cumulative aggregates are computed at delivery time by
    summing across fill records. ALP-462 — USD fields are :class:`Money`
    (Decimal-backed).
    """

    model_config = ConfigDict(frozen=True)

    regt_margin_before: Money
    regt_margin_after: Money
    regt_marginal_consumption: Money
    pm_equivalent_before: Money
    pm_equivalent_after: Money
    pm_marginal_consumption: Money
    regt_excess_over_pm: Money
    pm_model_version: str


class FillRecord(BaseModel):
    """Frozen typed handle for a ``fill_records`` row.

    Mirrors the column shape in
    ``docs/design/05-execution-layer/state-persistence.md`` § Fill records.
    The ``processing_status`` lifecycle is enforced at construction time:
    callers cannot build a record where the status disagrees with the
    integration-metadata fields.
    """

    model_config = ConfigDict(frozen=True)

    fill_id: str
    order_id: str
    fill_timestamp: datetime
    fill_price: Price
    fill_quantity: float
    remaining_quantity_after: float
    order_status_after: OrderStatus
    slippage_usd: Money | None
    fees_usd: Money
    execution_venue: str | None
    gateway_reference: str | None
    persistence_timestamp: datetime
    processing_status: FillProcessingStatus
    processing_invocation_id: str | None
    processing_timestamp: datetime | None
    regt_attribution: RegTMarginAttribution | None
    live_execution_estimate: LiveExecutionEstimate | None

    @model_validator(mode="after")
    def _check_processing_consistency(self) -> FillRecord:
        unprocessed = self.processing_status == FillProcessingStatus.UNPROCESSED
        has_invocation = self.processing_invocation_id is not None
        has_timestamp = self.processing_timestamp is not None
        if unprocessed and (has_invocation or has_timestamp):
            msg = "unprocessed fill must not carry processing_invocation_id or processing_timestamp"
            raise ValueError(msg)
        if not unprocessed and not (has_invocation and has_timestamp):
            msg = (
                f"{self.processing_status.value} fill requires both "
                "processing_invocation_id and processing_timestamp"
            )
            raise ValueError(msg)
        return self


__all__ = [
    "CorporateActionLedgerStatus",
    "FillProcessingStatus",
    "FillRecord",
    "RegTMarginAttribution",
]
