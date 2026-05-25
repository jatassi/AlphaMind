"""First-run bootstrap of the cash_ledger + drawdown_state singletons (ALP-620).

When the operator flips the switch on a freshly-reset Alpaca paper account,
the local DB has no ``cash_ledger`` row and no ``drawdown_state`` row.
Downstream consumers (the strategist's portfolio bundle, the breach
evaluator's HWM check, the synthesizer's exposure summary) read both
singletons and produce malformed output on the empty case.

This module fetches Alpaca's authoritative cash balance via the broker
adapter and writes the two singleton rows so the rest of the pipeline can
run from cold start. The bootstrap is a one-shot operation gated behind
``--fresh-start`` on the scheduler CLI.

The reconciliation auto-correct path (ALP-619) handles drift on *existing*
``cash_ledger`` rows but explicitly short-circuits when the row is absent
(``_reconcile_cash`` returns 0 on ``cash_row is None``); ALP-620 fills the
cold-start gap that ALP-619 leaves open.

Hard-fail preconditions:

* Alpaca reports any positions — the flag is for genuinely-empty accounts.
  An existing position means the operator should either reset the paper
  account first or reconcile out-of-band; honestly materializing a local
  ``PositionRecord`` (with thesis_id + cost basis + execution history)
  from the snapshot isn't possible, so synthesizing one here would corrupt
  the activity log.
* ``cash_ledger`` already has a row — a populated row implies a prior
  invocation, and silently overwriting it would clobber the live cash
  state. The reconciliation auto-correct path handles drift on populated
  rows.

The build helpers mirror :mod:`alphamind.scheduler.debug_e2e.seed`'s
``_build_cash_ledger_row`` / ``_build_drawdown_state_row` one-for-one; the
duplication is required because the import-linter contract
``debug-e2e-forbidden-in-production`` blocks production scheduler code
from reaching into the debug-e2e package.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter.client_factory import (
    AlpacaClientFactory,
)
from alphamind.execution.broker_adapter.client_factory import (
    ExecutionMode as ClientFactoryExecutionMode,
)
from alphamind.execution.broker_adapter.protocols import AccountStateQueriesP
from alphamind.execution.broker_adapter.queries import (
    AccountStateQueries,
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)

__all__ = [
    "FreshStartPreconditionError",
    "bootstrap_singletons_from_alpaca",
    "run_fresh_start_bootstrap",
]

log = logging.getLogger(__name__)

_AccountQueriesFactory = Callable[[VenueConfig, ExecutionMode], AccountStateQueriesP]


class FreshStartPreconditionError(RuntimeError):
    """Raised when ``--fresh-start`` preconditions are not met.

    The CLI surfaces the message verbatim to the operator (no traceback
    swallow); the outermost ``BaseException`` handler in ``__main__``
    catches it but only after the message has been logged.
    """


def _default_account_queries_factory(
    venue_config: VenueConfig, execution_mode: ExecutionMode
) -> AccountStateQueriesP:
    """Construct an Alpaca-backed ``AccountStateQueries`` from the venue config.

    Mirrors :func:`alphamind.scheduler.phase1_inputs._default_account_queries_factory`
    inline rather than importing the phase1-private helper — keeping each
    module's broker construction in-module avoids tightening the coupling
    between the bootstrap path and the in-invocation gatherer.
    """
    mode_literal: ClientFactoryExecutionMode = (
        "live" if execution_mode is ExecutionMode.live else "paper"
    )
    factory = AlpacaClientFactory(venue_config, mode=mode_literal)
    return AccountStateQueries(factory.build_trading_client())


def _build_cash_ledger_row(cash: Decimal, *, now: datetime) -> CashLedgerRow:
    """Build the singleton ``cash_ledger`` row from Alpaca's reported cash.

    Mirror of :func:`alphamind.scheduler.debug_e2e.seed._build_cash_ledger_row`
    — see the module docstring for why the duplication is required.

    On a freshly-reset account ``settled_cash`` equals ``current_cash``
    (no unsettled proceeds yet) and ``reserved_capital`` /
    ``margin_held`` are zero (no open orders, no margin loans).
    ``available_buying_power`` mirrors ``current_cash``; the snapshot
    assembler recomputes it at read time using the canonical
    ``settled - reserved - margin_held`` formula.
    """
    zero = Decimal(0)
    return CashLedgerRow(
        id=CASH_LEDGER_SINGLETON_ID,
        current_cash_usd=cash,
        settled_cash_usd=cash,
        reserved_capital_usd=zero,
        available_buying_power_usd=cash,
        margin_held_usd=zero,
        unsettled_proceeds_json="[]",
        last_updated_at=now.isoformat(),
    )


def _build_drawdown_state_row(cash: Decimal, *, now: datetime) -> DrawdownStateRow:
    """Build the singleton ``drawdown_state`` row from Alpaca's reported cash.

    Mirror of :func:`alphamind.scheduler.debug_e2e.seed._build_drawdown_state_row`
    — see the module docstring.

    With zero positions, ``equity == cash``; seeding
    ``equity_high_water_mark_usd`` at the Alpaca cash value anchors the
    HWM at the starting equity so the first invocation's drawdown
    computation reads a sane baseline (zero drawdown from a zero-position
    starting equity).
    """
    return DrawdownStateRow(
        id=DRAWDOWN_STATE_SINGLETON_ID,
        equity_high_water_mark_usd=float(cash),
        current_drawdown_pct=0.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        drawdown_by_source_json="{}",
        last_updated_at=now.isoformat(),
    )


async def bootstrap_singletons_from_alpaca(
    *,
    session: AsyncSession,
    account: TradeAccountSnapshot,
    positions: tuple[PositionSnapshot, ...],
    now: datetime,
) -> None:
    """Insert ``cash_ledger`` + ``drawdown_state`` from an Alpaca snapshot.

    Raises :class:`FreshStartPreconditionError` if Alpaca reports any
    positions or if ``cash_ledger`` already has a row. Does NOT commit —
    the caller owns the transaction so the bootstrap and any preceding /
    following writes are atomic.
    """
    if positions:
        symbols = ", ".join(sorted(pos.symbol for pos in positions))
        msg = (
            f"--fresh-start refuses to run: Alpaca reports {len(positions)} "
            f"open position(s) ({symbols}). The flag is for genuinely-empty "
            "accounts. Reset the Alpaca paper account first, or rely on the "
            "reconciliation auto-correct path (ALP-619) once the cash_ledger "
            "singleton is populated by hand."
        )
        raise FreshStartPreconditionError(msg)

    existing_cash_row = await session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if existing_cash_row is not None:
        msg = (
            "--fresh-start refuses to run: cash_ledger already initialized "
            f"(current_cash_usd={existing_cash_row.current_cash_usd}). The "
            "flag is for first-run only; a populated cash_ledger row implies "
            "a prior invocation. Use the reconciliation auto-correct path "
            "(ALP-619) to reconcile drift instead."
        )
        raise FreshStartPreconditionError(msg)

    cash_row = _build_cash_ledger_row(account.cash, now=now)
    drawdown_row = _build_drawdown_state_row(account.cash, now=now)
    session.add(cash_row)
    session.add(drawdown_row)
    log.info(
        "fresh-start bootstrap: cash_ledger.current_cash_usd=%s, "
        "drawdown_state.equity_high_water_mark_usd=%s",
        cash_row.current_cash_usd,
        drawdown_row.equity_high_water_mark_usd,
    )


async def run_fresh_start_bootstrap(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    now: datetime,
    account_queries_factory: _AccountQueriesFactory | None = None,
) -> None:
    """Top-level entry: fetch Alpaca state, validate, write singletons, commit.

    The CLI calls this exactly once before ``record_process_lifetime`` so
    the bootstrap rows are persisted in their own transaction; if the
    bootstrap fails the operator sees the precondition error before any
    invocation row is opened.

    ``account_queries_factory`` is the test seam — production callers omit
    it and the inline Alpaca-backed default runs.
    """
    factory = account_queries_factory or _default_account_queries_factory
    queries = factory(venue_config, execution_mode)
    account = queries.get_account()
    positions = queries.get_positions()
    async with session_factory() as session:
        await bootstrap_singletons_from_alpaca(
            session=session,
            account=account,
            positions=positions,
            now=now,
        )
        await session.commit()
