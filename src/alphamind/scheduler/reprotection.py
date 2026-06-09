"""Post-fill-collection re-bracket step (ALP-938).

Orchestrates the auto re-protection of partial-close remainders across the layer
boundary in three ALP-824-safe phases:

1. **Gather** — read the ``reprotection_needed=1`` candidates in a read session,
   no write lock held.
2. **Submit** — submit a fresh standalone OCO per candidate with the broker
   ``TradingClient``, again with NO write lock held (the broker network I/O must
   never run under ``begin_write_immediate``).
3. **Persist** — append the re-bracketed legs and clear the markers in the step's
   OWN write transaction (``begin_write_immediate`` + ``run_with_sqlite_busy_retry``),
   opened only after the broker calls have all returned.

The DB halves (gather / persist) live in
``execution/write_paths/reprotection.py``; this module holds the broker client, the
conforming pipeline-protective ``client_order_id``, and the submit-failure alert. It
never raises out of the orchestrator: a submit failure alerts and leaves the marker
set (the next invocation retries), and any unexpected error is logged and swallowed,
so re-protection can never abort the pipeline invocation. The continuous-monitor
position-level max-loss guardrail backstops the brief unprotected window.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter import (
    EquityLegAck,
    GatewaySubmissionFailed,
    Submitted,
    submit_equity_oco,
)
from alphamind.execution.broker_adapter.order_equity import EquitySubmission
from alphamind.execution.broker_adapter.retry import SubmissionOutcome
from alphamind.execution.oms.command_ids import derive_pipeline_protective_command_id
from alphamind.execution.write_paths.reprotection import (
    ReprotectionCandidate,
    gather_reprotection_candidates,
    persist_reprotection,
)
from alphamind.persistence.retry import run_with_sqlite_busy_retry
from alphamind.persistence.session import begin_write_immediate
from alphamind.scheduler.fill_collection_inputs import _alpaca_client_factory
from alphamind.state.invocation_context.context import InvocationHandle

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

logger = logging.getLogger(__name__)


def _default_trading_client_factory(
    venue_config: VenueConfig, execution_mode: ExecutionMode
) -> TradingClient:
    """Resolve the venue config + execution mode into a live broker ``TradingClient``.

    The production builder for :func:`run_reprotection_step`'s broker submit. Tests
    inject a substitute via the step's ``trading_client_factory`` parameter, mirroring
    ``gather_fill_collection_inputs(account_queries_factory=…)`` — so the broker
    network client is faked at the sanctioned Alpaca boundary, never under the write
    lock and never against the real API.
    """
    return _alpaca_client_factory(venue_config, execution_mode).build_trading_client()


@dataclass(frozen=True)
class _SubmittedReprotection:
    """A candidate whose OCO submit succeeded (or was skipped in debug_e2e mode),
    ready for the persist phase."""

    candidate: ReprotectionCandidate
    client_order_id: str
    leg_acks: tuple[EquityLegAck, ...]
    broker_enforced: bool


async def run_reprotection_step(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    execution_config: ExecutionConfig,
    broker_routing_active: bool,
    now: datetime,
    trading_client_factory: Callable[[VenueConfig, ExecutionMode], TradingClient] | None = None,
) -> int:
    """Re-protect every naked partial-close remainder; return the count persisted.

    Gather (read) → submit (broker, no lock) → persist (own write transaction). When
    ``broker_routing_active`` is False (debug_e2e / log-only) no broker call is made
    and the legs persist monitor-enforced with NULL broker ids, mirroring the OPEN
    path. Never raises out of the orchestrator.

    ``trading_client_factory`` defaults to :func:`_default_trading_client_factory`
    (the live Alpaca builder); tests inject a substitute to fake the broker at the
    sanctioned boundary.
    """
    try:
        # --- 1. Gather (read session, no write lock) ---
        async with session_factory() as read_session:
            read_handle = InvocationHandle(session=read_session, invocation_id=invocation_id)
            candidates = await gather_reprotection_candidates(read_handle)
        if not candidates:
            return 0

        # --- 2. Submit (broker client, no write lock) ---
        build_client = trading_client_factory or _default_trading_client_factory
        client = build_client(venue_config, execution_mode) if broker_routing_active else None
        submitted: list[_SubmittedReprotection] = []
        for candidate in candidates:
            client_order_id = derive_pipeline_protective_command_id(
                position_id=candidate.position_id,
                thesis_id=candidate.thesis_id,
                invocation_id=invocation_id,
            )
            if client is None:
                # debug_e2e / log-only: persist monitor-enforced legs (NULL broker ids).
                submitted.append(
                    _SubmittedReprotection(candidate, client_order_id, (), broker_enforced=False)
                )
                continue
            result = await _submit_one(
                candidate,
                client=client,
                execution=execution_config,
                client_order_id=client_order_id,
            )
            if result is not None:
                submitted.append(result)
        if not submitted:
            return 0

        # --- 3. Persist (own write transaction, opened after all broker calls) ---
        async def _persist_unit() -> int:
            async with session_factory() as write_session:
                await begin_write_immediate(write_session)
                write_handle = InvocationHandle(session=write_session, invocation_id=invocation_id)
                for entry in submitted:
                    await persist_reprotection(
                        write_handle,
                        candidate=entry.candidate,
                        client_order_id=entry.client_order_id,
                        leg_acks=entry.leg_acks,
                        broker_enforced=entry.broker_enforced,
                        timestamp=now,
                    )
                await write_session.commit()
                return len(submitted)

        return await run_with_sqlite_busy_retry(_persist_unit)
    except Exception:
        # The step must never abort the invocation. The markers stay set, so the
        # next invocation's step retries; the monitor max-loss guardrail backstops.
        logger.critical(
            "ALP-938 reprotection step failed unexpectedly; reprotection markers left "
            "set for retry next invocation. The continuous-monitor max-loss guardrail is "
            "the active backstop.",
            exc_info=True,
        )
        return 0


async def _submit_one(
    candidate: ReprotectionCandidate,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> _SubmittedReprotection | None:
    """Submit one candidate's OCO; alert + return None on failure (marker stays set)."""
    try:
        outcome: SubmissionOutcome[EquitySubmission] = await submit_equity_oco(
            client=client,
            execution=execution,
            client_order_id=client_order_id,
            symbol=candidate.ticker,
            qty=candidate.remaining_qty,
            position_side=candidate.position_side,
            levels=candidate.levels,
        )
    except Exception:
        # A permanent broker rejection raises out of submit_with_retry. Alert and
        # leave the marker set rather than letting it abort the invocation.
        logger.exception("reprotection: OCO submit raised for %s", candidate.position_id)
        _alert_reprotection_failed(candidate, reason="permanent rejection at submit")
        return None
    if isinstance(outcome, GatewaySubmissionFailed):
        _alert_reprotection_failed(candidate, reason=f"gateway failure ({outcome.reason})")
        return None
    assert isinstance(outcome, Submitted)
    return _SubmittedReprotection(
        candidate, client_order_id, outcome.payload.leg_acks, broker_enforced=True
    )


def _alert_reprotection_failed(candidate: ReprotectionCandidate, *, reason: str) -> None:
    """Emit the ALP-938 (G) broker-naked-remainder operator alert (mirrors ALP-937 (F))."""
    logger.critical(
        "ALP-938 NAKED POSITION: re-bracket OCO submit FAILED for %s (position_id=%s): %s. "
        "The remaining %s shares stay broker-unprotected; reprotection_needed is left set so "
        "the next invocation retries. The continuous-monitor position-level max-loss guardrail "
        "is the active backstop.",
        candidate.ticker,
        candidate.position_id,
        reason,
        candidate.remaining_qty,
    )


__all__ = ["run_reprotection_step"]
