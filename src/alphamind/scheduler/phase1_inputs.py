"""Phase 1 input gatherer (story 03b / ALP-445; refactored ALP-494).

Assembles the typed bundle ``process_unprocessed_fills`` consumes — Alpaca
account + positions, v1beta1 corporate-action activities, and the
``MarketInputs`` the per-fill Reg T margin attribution wedge requires.

Per parent decision (H), broker-side failures degrade the bundle (no-op
defaults + ``staleness_flag=True``) rather than aborting the invocation;
exceptions raised by individual data-fetch helpers are caught and logged.
The orchestrator (``run_invocation``) reads ``Phase1Inputs.staleness_flag``
to populate the row's ``staleness_flag`` column.

ALP-494 — broker-adapter access is parameterized via optional
``account_queries_factory`` / ``ca_queries_factory`` kwargs typed against
the structural Protocols in
:mod:`alphamind.execution.broker_adapter.protocols`. Production callers
omit both and the gatherer constructs Alpaca-backed defaults inline; the
debug-e2e package (story 02b) and tests inject log-only / stub
implementations through the same seam. Retires the module-level
``_build_*`` monkey-patch hooks story 03b ships with.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import VenueConfig
from alphamind.distillation.realized_vol import read_realized_vol_map
from alphamind.execution.broker_adapter.client_factory import (
    AlpacaClientFactory,
)
from alphamind.execution.broker_adapter.client_factory import (
    ExecutionMode as ClientFactoryExecutionMode,
)
from alphamind.execution.broker_adapter.corporate_actions_queries import (
    CorporateActionsQueries,
)
from alphamind.execution.broker_adapter.protocols import (
    AccountStateQueriesP,
    CorporateActionsQueriesP,
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
from alphamind.persistence.models import AssetUniverse, MacroObservations, OhlcvBars
from alphamind.portfolio_state.records.positions import Direction
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
    RealizedVolEntry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)

_AccountQueriesFactory = Callable[[VenueConfig, ExecutionMode], AccountStateQueriesP]
_CorporateActionsQueriesFactory = Callable[[VenueConfig, ExecutionMode], CorporateActionsQueriesP]

__all__ = ["Phase1Inputs", "gather_phase1_inputs"]

log = logging.getLogger(__name__)


# DTB3 (3-month Treasury bill) is the conventional macro-table series id for
# the short-end risk-free rate. The fallback is a sane mid-cycle scalar so
# the bootstrap path (empty macro_observations) does not blow up the Reg T
# wedge — operators backfill the table separately.
_DTB3_SERIES_ID = "DTB3"
_DEFAULT_RISK_FREE_RATE = 0.045


# ALP-587 — the OHLCV daily-bar timeframe key. The latest ``1d`` bar's close
# is the spot price used to guardrail-project active-universe tickers that
# are not in the held book.
_OHLCV_DAILY_TIMEFRAME = "1d"

# ALP-587 — staleness ceiling for an EOD bar consumed as a spot price. An
# EOD close is structurally staler than the live-quote freshness window
# (``config.snapshot_freshness_max_price_age_seconds`` is 900s) — applying
# that bound would reject every EOD bar. A daily bar prints once per
# trading session, so a seven-calendar-day ceiling clears the longest U.S.
# market-holiday weekend yet still drops a ticker whose price feed has
# genuinely stalled; a dropped ticker correctly falls back to the
# validation tool's ``UNAVAILABLE`` / ``missing_market_price`` outcome.
_MAX_EOD_BAR_AGE_SECONDS = 7 * 24 * 60 * 60


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


def _default_account_queries_factory(
    venue_config: VenueConfig, execution_mode: ExecutionMode
) -> AccountStateQueriesP:
    """Default Alpaca-backed ``AccountStateQueriesP`` construction.

    Used when ``gather_phase1_inputs`` is called without an
    ``account_queries_factory`` kwarg (the production daemon path).
    """
    mode_literal: ClientFactoryExecutionMode = (
        "live" if execution_mode is ExecutionMode.live else "paper"
    )
    factory = AlpacaClientFactory(venue_config, mode=mode_literal)
    return AccountStateQueries(factory.build_trading_client())


def _default_ca_queries_factory(
    venue_config: VenueConfig, execution_mode: ExecutionMode
) -> CorporateActionsQueriesP:
    """Default Alpaca-backed ``CorporateActionsQueriesP`` construction.

    Used when ``gather_phase1_inputs`` is called without a
    ``ca_queries_factory`` kwarg (the production daemon path).
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


def _bar_age_seconds(period_start: str, *, as_of: datetime) -> float:
    """Seconds between an ``ohlcv_bars.period_start`` and ``as_of``.

    ``period_start`` is the ISO-8601 string the OHLCV collector writes
    (``datetime.isoformat()`` of a tz-aware UTC instant). A value that
    parses without tzinfo is assumed UTC so the subtraction never raises
    on a naive/aware mismatch.
    """
    parsed = datetime.fromisoformat(period_start)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return (reference - parsed).total_seconds()


async def _read_active_universe_prices(
    session: AsyncSession,
    *,
    as_of: datetime,
    max_bar_age_seconds: float = _MAX_EOD_BAR_AGE_SECONDS,
) -> dict[str, float]:
    """Latest EOD close per active-universe ticker (ALP-587).

    Reads the most recent ``1d`` :class:`OhlcvBars` row for every ticker
    the ``asset_universe`` table marks ``is_active`` and returns
    ``{ticker: unadj_close}``. The *unadjusted* close — the actual traded
    price, not the split-adjusted series — is used so the value carries
    the same semantics as a broker ``current_price`` quote.

    Bars older than ``max_bar_age_seconds`` are dropped: the validation
    tool's ``UNAVAILABLE`` / ``missing_market_price`` path is the correct
    fallback for a ticker whose price feed has genuinely stalled.

    The decision agents propose against the full active universe, of which
    the held book priced by :func:`_build_market_inputs` is a strict
    subset. Held-position quotes still win on overlap — see the merge in
    that function.
    """
    latest_rank = (
        func.row_number()
        .over(
            partition_by=OhlcvBars.ticker,
            order_by=OhlcvBars.period_start.desc(),
        )
        .label("rn")
    )
    ranked = (
        select(
            OhlcvBars.ticker.label("ticker"),
            OhlcvBars.unadj_close.label("close"),
            OhlcvBars.period_start.label("period_start"),
            latest_rank,
        )
        .join(AssetUniverse, AssetUniverse.ticker == OhlcvBars.ticker)
        .where(
            OhlcvBars.timeframe == _OHLCV_DAILY_TIMEFRAME,
            AssetUniverse.is_active == 1,
        )
        .subquery()
    )
    stmt = select(ranked.c.ticker, ranked.c.close, ranked.c.period_start).where(ranked.c.rn == 1)
    rows = (await session.execute(stmt)).all()
    return {
        str(ticker): float(close)
        for ticker, close, period_start in rows
        if _bar_age_seconds(period_start, as_of=as_of) <= max_bar_age_seconds
    }


def _build_market_inputs(
    *,
    positions: tuple[PositionSnapshot, ...],
    universe_prices: Mapping[str, float],
    risk_free_rate: float,
    as_of: datetime,
    realized_vol_map: dict[str, float],
) -> MarketInputs:
    """Compose ``MarketInputs`` from universe + broker-position prices.

    ``underlying_prices`` merges two layers: ``universe_prices`` — latest
    EOD closes for every active-universe ticker (ALP-587) — forms the base,
    and each held position's ``current_price`` overrides it on overlap (a
    live broker quote beats an EOD bar). The base layer lets the decision
    agents validate proposals against active-universe tickers they do not
    yet hold; without it the validation tool returns ``UNAVAILABLE`` /
    ``missing_market_price`` for any unheld candidate.

    The IV provider's ``surface`` is empty (no production options-chain
    producer yet); the ``realized_vol`` mapping is the per-underlying
    trailing-30d scalar produced by ALP-530's distillation hook, wrapped at
    this boundary into ``RealizedVolEntry`` records. Underlyings without a
    row are absent from the mapping — the consumer's fallback chain emits
    ``IvLookupError`` for those.
    """
    # ALP-462 — ``pos.current_price`` is ``Price`` (Decimal) on the
    # PositionSnapshot boundary; cast at the legacy MarketInputs surface which
    # still uses float (risk_guardrails/guardrail_evaluation/types is outside ALP-462).
    position_prices: dict[str, float] = {
        pos.symbol: float(pos.current_price) for pos in positions if pos.current_price is not None
    }
    # ALP-587 — universe EOD closes are the base layer; held-position live
    # quotes take precedence where a ticker appears in both.
    underlying_prices: dict[str, float] = {**universe_prices, **position_prices}
    realized_vol_entries = {
        ticker: RealizedVolEntry(underlying=ticker, trailing_30d_realized_vol=vol)
        for ticker, vol in realized_vol_map.items()
    }
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=risk_free_rate,
        iv_provider=FixtureIvProvider(surface={}, realized_vol=realized_vol_entries),
        as_of=as_of,
    )


async def gather_phase1_inputs(
    *,
    handle: InvocationHandle,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    as_of: datetime,
    account_queries_factory: _AccountQueriesFactory | None = None,
    ca_queries_factory: _CorporateActionsQueriesFactory | None = None,
) -> Phase1Inputs:
    """Assemble the Phase 1 input bundle for ``process_unprocessed_fills``.

    Calls the broker adapter for account + positions, the v1beta1 fetcher
    for CA activities, the macro table for the risk-free rate, and
    ``ohlcv_bars`` for the active-universe EOD closes that price the
    decision agents' validation tool (ALP-587). Each broker call is
    independently wrapped: a ``RuntimeError`` from any sub-fetch degrades
    the corresponding field to a no-op default and flips
    ``staleness_flag`` to ``True``; the function never raises.

    The ``InvocationHandle`` carries the open transaction the v1beta1
    fetcher needs to consult the CA integration ledger. Macro-table reads
    join the same transaction so the snapshot is internally consistent.

    ``account_queries_factory`` / ``ca_queries_factory`` are optional
    Protocol-typed seams (ALP-494). When ``None`` (the production daemon
    path), the inline Alpaca-backed defaults run. Story 02b's log-only
    queries and the test suite pass their own factory to substitute the
    real broker without monkey-patching.
    """
    account_factory = account_queries_factory or _default_account_queries_factory
    ca_factory = ca_queries_factory or _default_ca_queries_factory

    staleness_flag = False

    account: TradeAccountSnapshot | None
    positions: tuple[PositionSnapshot, ...]
    try:
        queries = account_factory(venue_config, execution_mode)
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
            ca_queries = ca_factory(venue_config, execution_mode)
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

    # ALP-530 — fetch per-underlying realized-vol scalars for the open
    # position set, restricted to those symbols so the read scans the
    # smallest possible window. Underlyings without a row are absent from
    # the returned dict; the Reg T wedge tolerates missing entries by
    # routing them through the IV-fallback's terminal error path.
    realized_vol_map = await read_realized_vol_map(
        handle.session,
        tickers=tuple(pos.symbol for pos in positions),
    )

    # ALP-587 — latest EOD closes for the full active universe, so the
    # decision agents' validation tool can price proposals against tickers
    # that are in the universe but not yet held.
    universe_prices = await _read_active_universe_prices(handle.session, as_of=as_of)

    market_inputs = _build_market_inputs(
        positions=positions,
        universe_prices=universe_prices,
        risk_free_rate=risk_free_rate,
        as_of=as_of,
        realized_vol_map=dict(realized_vol_map),
    )

    return Phase1Inputs(
        ca_activities=ca_activities,
        alpaca_positions=positions,
        alpaca_account=account,
        market_inputs=market_inputs,
        staleness_flag=staleness_flag,
    )
