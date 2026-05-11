"""Phase 1 input gatherer (story 03b / ALP-445).

Assembles the typed bundle ``process_unprocessed_fills`` consumes — Alpaca
account + positions, v1beta1 corporate-action activities, and the
``MarketInputs`` the per-fill Reg T margin attribution wedge requires.

Per parent decision (H), broker-side failures degrade the bundle (no-op
defaults + ``staleness_flag=True``) rather than aborting the invocation;
exceptions raised by individual data-fetch helpers are caught and logged.
The orchestrator (``run_invocation``) reads ``Phase1Inputs.staleness_flag``
to populate the row's ``staleness_flag`` column.

The two ``_build_*`` factory hooks at module level are seams: production
constructs ``AccountStateQueries`` / ``CorporateActionsQueries`` against
the venue config; tests monkey-patch them to inject stub queries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter.client_factory import (
    AlpacaClientFactory,
)
from alphamind.execution.broker_adapter.client_factory import (
    ExecutionMode as ClientFactoryExecutionMode,
)
from alphamind.execution.broker_adapter.corporate_actions_queries import (
    CorporateActionsQueries,
)
from alphamind.execution.broker_adapter.queries import (
    AccountStateQueries,
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.corporate_actions.config import CorporateActionsConfig
from alphamind.execution.corporate_actions.fetcher import (
    fetch_unprocessed_ca_activities,
)
from alphamind.execution.corporate_actions.types import (
    CorporateActionActivity,
    PositionLookup,
)
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.persistence.models import MacroObservations
from alphamind.portfolio_state.records.positions import Direction
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)

__all__ = ["Phase1Inputs", "gather_phase1_inputs"]

log = logging.getLogger(__name__)


# DTB3 (3-month Treasury bill) is the conventional macro-table series id for
# the short-end risk-free rate. The fallback is a sane mid-cycle scalar so
# the bootstrap path (empty macro_observations) does not blow up the Reg T
# wedge — operators backfill the table separately.
_DTB3_SERIES_ID = "DTB3"
_DEFAULT_RISK_FREE_RATE = 0.045


@dataclass(frozen=True, slots=True)
class Phase1Inputs:
    """Bundle the orchestrator hands to ``process_unprocessed_fills``.

    Each field can independently degrade to a no-op default when its
    upstream fetch raises ``RuntimeError``; ``staleness_flag`` flips to
    ``True`` in that case and surfaces on the ``InvocationSummary``.
    """

    ca_activities: tuple[CorporateActionActivity, ...]
    alpaca_positions: tuple[PositionSnapshot, ...]
    alpaca_account: TradeAccountSnapshot | None
    market_inputs: MarketInputs
    staleness_flag: bool


def _build_account_state_queries(
    venue_config: VenueConfig, execution_mode: ExecutionMode
) -> AccountStateQueries:
    """Construct ``AccountStateQueries`` from venue config + execution mode.

    Module-level seam: tests monkey-patch this to inject a stub queries
    object so the gatherer's broker-adapter path can be exercised without
    real Alpaca credentials.
    """
    mode_literal: ClientFactoryExecutionMode = (
        "live" if execution_mode is ExecutionMode.live else "paper"
    )
    factory = AlpacaClientFactory(venue_config, mode=mode_literal)
    return AccountStateQueries(factory.build_trading_client())


def _build_corporate_actions_queries(
    venue_config: VenueConfig, execution_mode: ExecutionMode
) -> CorporateActionsQueries:
    """Construct ``CorporateActionsQueries`` from venue config + execution mode.

    Symmetric seam to :func:`_build_account_state_queries`; tests inject a
    stub so the gatherer composes without hitting Alpaca.
    """
    mode_literal: ClientFactoryExecutionMode = (
        "live" if execution_mode is ExecutionMode.live else "paper"
    )
    factory = AlpacaClientFactory(venue_config, mode=mode_literal)
    return CorporateActionsQueries(factory.build_corporate_actions_client())


def _position_lookup_from_positions(
    positions: tuple[PositionSnapshot, ...],
) -> dict[str, PositionLookup]:
    """Build a per-symbol ``PositionLookup`` map from alpaca-side positions.

    Direction is derived from the broker's ``side`` field; ``quantity``
    carries the absolute share count. The lookup is consumed by the v1beta1
    fetcher to attach cash-dividend amounts to held tickers. ``position_id``
    is sourced from the broker's symbol — the CA fetcher's persistence layer
    treats it as a per-symbol identifier in the dividend-pricing path.
    """
    return {
        pos.symbol: PositionLookup(
            position_id=pos.symbol,
            quantity=abs(pos.qty),
            direction=Direction.LONG if pos.side == "long" else Direction.SHORT,
        )
        for pos in positions
    }


async def _read_latest_risk_free_rate(handle: InvocationHandle) -> float:
    """Read the most recent DTB3 observation from ``macro_observations``.

    The 3-month Treasury bill rate is the canonical short-end risk-free
    rate the Reg T attribution model consumes. Values in the table are
    typically expressed in percent (e.g., ``4.5`` for 4.5%); the rate is
    divided by 100 before returning. When the table is empty (bootstrap
    path), the conservative mid-cycle scalar :data:`_DEFAULT_RISK_FREE_RATE`
    is returned.
    """
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.series_id == _DTB3_SERIES_ID,
            MacroObservations.value.is_not(None),
        )
        .order_by(MacroObservations.observation_date.desc())
        .limit(1)
    )
    result = await handle.session.execute(stmt)
    value = result.scalar_one_or_none()
    if value is None:
        return _DEFAULT_RISK_FREE_RATE
    return float(value) / 100.0


def _build_market_inputs(
    *,
    positions: tuple[PositionSnapshot, ...],
    risk_free_rate: float,
    as_of: datetime,
) -> MarketInputs:
    """Compose ``MarketInputs`` from broker positions + macro rate.

    ``underlying_prices`` map is populated from each position's
    ``current_price`` (skipping positions with ``None``). The IV provider
    is a no-op ``FixtureIvProvider`` — equities-only Phase 1 (and tests)
    do not consume a real surface; future stories can swap in a backed
    options-chain provider without changing this signature.
    """
    underlying_prices: dict[str, float] = {
        pos.symbol: pos.current_price for pos in positions if pos.current_price is not None
    }
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=risk_free_rate,
        iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        as_of=as_of,
    )


async def gather_phase1_inputs(
    *,
    handle: InvocationHandle,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    as_of: datetime,
) -> Phase1Inputs:
    """Assemble the Phase 1 input bundle for ``process_unprocessed_fills``.

    Calls the broker adapter for account + positions, the v1beta1 fetcher
    for CA activities, and the macro table for the risk-free rate. Each
    call is independently wrapped: a ``RuntimeError`` from any sub-fetch
    degrades the corresponding field to a no-op default and flips
    ``staleness_flag`` to ``True``; the function never raises.

    The ``InvocationHandle`` carries the open transaction the v1beta1
    fetcher needs to consult the CA integration ledger. Macro-table reads
    join the same transaction so the snapshot is internally consistent.
    """
    staleness_flag = False

    account: TradeAccountSnapshot | None
    positions: tuple[PositionSnapshot, ...]
    try:
        queries = _build_account_state_queries(venue_config, execution_mode)
    except RuntimeError as exc:
        log.warning(
            "phase1_inputs: broker-adapter construction failed (%s); degrading "
            "alpaca_account/alpaca_positions to defaults",
            exc,
        )
        account = None
        positions = ()
        staleness_flag = True
    else:
        try:
            account = queries.get_account()
        except RuntimeError as exc:
            log.warning(
                "phase1_inputs: AccountStateQueries.get_account() failed (%s); "
                "degrading alpaca_account to None",
                exc,
            )
            account = None
            staleness_flag = True
        try:
            positions = queries.get_positions()
        except RuntimeError as exc:
            log.warning(
                "phase1_inputs: AccountStateQueries.get_positions() failed (%s); "
                "degrading alpaca_positions to empty tuple",
                exc,
            )
            positions = ()
            staleness_flag = True

    ca_activities: tuple[CorporateActionActivity, ...] = ()
    if positions:
        try:
            ca_queries = _build_corporate_actions_queries(venue_config, execution_mode)
            lookup_map = _position_lookup_from_positions(positions)
            ca_activities = await fetch_unprocessed_ca_activities(
                handle,
                ca_queries,
                config=CorporateActionsConfig(),
                position_lookup_for_symbol=lookup_map.get,
                known_symbols=tuple(sorted(lookup_map)),
            )
        except RuntimeError as exc:
            log.warning(
                "phase1_inputs: corporate-actions fetch failed (%s); degrading "
                "ca_activities to empty tuple",
                exc,
            )
            ca_activities = ()
            staleness_flag = True

    try:
        risk_free_rate = await _read_latest_risk_free_rate(handle)
    except RuntimeError as exc:
        log.warning(
            "phase1_inputs: macro_observations DTB3 read failed (%s); "
            "falling back to default risk-free rate",
            exc,
        )
        risk_free_rate = _DEFAULT_RISK_FREE_RATE
        staleness_flag = True

    market_inputs = _build_market_inputs(
        positions=positions, risk_free_rate=risk_free_rate, as_of=as_of
    )

    return Phase1Inputs(
        ca_activities=ca_activities,
        alpaca_positions=positions,
        alpaca_account=account,
        market_inputs=market_inputs,
        staleness_flag=staleness_flag,
    )
