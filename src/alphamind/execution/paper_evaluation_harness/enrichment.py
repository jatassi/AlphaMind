"""Per-fill enrichment wedge for the paper-evaluation harness (ALP-528).

The wedge sits between the fill-stream consumer's translator
(``fill_report_to_fill_record``) and the persistence layer
(``append_fill_record``). It looks up the per-fill harness inputs — order
attributes from the ``orders`` table, ADV from the distillation repository,
realized volatility from the per-underlying map — calls the harness
composition, and attaches the result to the ``FillRecord`` via ``model_copy``.

The wedge is async because production lookups are DB-backed; tests substitute
in-memory fakes against the Protocols defined here. The harness composition
(``compute_live_execution_estimate``) remains pure — all I/O lives in the
adapters consumed at this seam.

The dependency direction is one-way: ``paper_evaluation_harness`` defines the
Protocols, and the wiring callsite in ``continuous_monitor/__main__.py``
constructs the production adapters and the partial-application callable that
``run_fill_stream_consumer`` receives. ``paper_evaluation_harness`` never
imports from ``continuous_monitor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from alphamind.config.models.execution import OrderType, PaperHarness
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.positions import InstrumentType
from alphamind.state.records import FillRecord

__all__ = [
    "AdvLookup",
    "OrderAttributes",
    "OrderLookup",
    "VolLookup",
    "attach_live_execution_estimate",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrderAttributes:
    """Per-order attributes the harness needs to compute a fill's drag.

    ``order_type`` uses the lowercase ``OrderType`` enum on the
    ``PaperHarness.impact_coefficients`` map (the harness consumes this enum
    directly). ``side`` is the buy/sell discriminator the harness uses for
    the live-adjusted price sign; ``OrderRecord.direction`` carries six
    OrderDirection values (BUY, SELL, BUY_TO_OPEN, …) which the production
    adapter normalises to "buy"/"sell" via its prefix. ``instrument_type``
    discriminates the EQUITY vs OPTIONS dispatch (STRATEGY parent fills are
    filtered upstream by the translator). ``ticker_or_underlying`` keys the
    ADV / vol lookups: the equity ticker for EQUITY fills, the underlying
    ticker for OPTIONS fills.
    """

    order_type: OrderType
    side: Literal["buy", "sell"]
    instrument_type: InstrumentType
    ticker_or_underlying: str


class OrderLookup(Protocol):
    """Async accessor for per-order attributes keyed by ``order_id``.

    Production impl reads from the ``orders`` table via the
    ``OrderRow``/``OrderRecord`` codec. In-memory fakes back this Protocol
    in unit tests.
    """

    async def get_order_attributes(self, order_id: str) -> OrderAttributes | None: ...


class AdvLookup(Protocol):
    """Async accessor for per-ticker average daily volume (shares).

    Production impl reads ``asset_universe.avg_daily_volume_shares`` (the
    same column ``SqlDistillationRepository.load_ticker_adv`` queries; the
    two adapters' queries diverged because the distillation repo uses a sync
    session and this wedge runs in the async monitor — consolidation tracked
    by ALP-533). Returns ``None`` when the ticker has no ADV substrate,
    which the harness propagates as ``LiveExecutionEstimate is None``.
    """

    async def get_adv_shares(self, ticker: str) -> float | None: ...


class VolLookup(Protocol):
    """Async accessor for per-underlying realized volatility (annualized).

    Production impl reads from the per-underlying realized-vol map shared
    with the breach-loop's ``SqlOptionsIvProvider`` (ALP-642 — the same
    map serves as that provider's surface-miss fallback channel). Returns
    ``None`` when no entry exists for the underlying — same fallback as
    ADV.
    """

    async def get_realized_volatility(self, underlying: str) -> float | None: ...


async def attach_live_execution_estimate(
    record: FillRecord,
    *,
    order_lookup: OrderLookup,
    adv_lookup: AdvLookup,
    vol_lookup: VolLookup,
    config: PaperHarness,
) -> FillRecord:
    """Enrich a translated ``FillRecord`` with the paper-mode live-execution
    estimate.

    Behavior:

    * Order lookup miss → return record unchanged, log a WARNING. (A fill
      whose ``order_id`` is not in the ``orders`` table is anomalous; the
      production order writer always persists the order before any fill
      reaches the translator.)
    * Any required harness input (ADV, vol) ``None`` → call the harness;
      it returns ``None`` and the record is returned unchanged.
    * Harness returns a ``LiveExecutionEstimate`` → return
      ``record.model_copy(update={"live_execution_estimate": estimate})``.

    The wedge runs once per persisted ``FillRecord``; the live-mode path
    bypasses the wedge entirely by passing ``enrichment_callable=None`` to
    :func:`run_fill_stream_consumer`.
    """
    attrs = await order_lookup.get_order_attributes(record.order_id)
    if attrs is None:
        log.warning(
            "attach_live_execution_estimate: order not found for fill; "
            "persisting without estimate. order_id=%s fill_id=%s",
            record.order_id,
            record.fill_id,
        )
        return record

    adv_shares = await adv_lookup.get_adv_shares(attrs.ticker_or_underlying)
    realized_vol = await vol_lookup.get_realized_volatility(attrs.ticker_or_underlying)

    estimate = compute_live_execution_estimate(
        fill_price=record.fill_price,
        fill_quantity=record.fill_quantity,
        instrument_type=attrs.instrument_type,
        side=attrs.side,
        order_type=attrs.order_type,
        adv_shares=adv_shares,
        realized_volatility=realized_vol,
        config=config,
    )
    if estimate is None:
        return record
    return record.model_copy(update={"live_execution_estimate": estimate})
