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
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

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
    IvProvider,
    MarketInputs,
    RealizedVolEntry,
    SqlOptionsIvProvider,
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


# ALP-747 — per-timeframe granularity rank used to break a ``period_start``
# tie when selecting the freshest reference bar. A top-of-hour open can carry a
# 15min and a 1h bar starting at the same instant; the finer-grained bar is the
# more recent price, so it wins (lower rank). The ranks mirror the timeframes
# the OHLCV collector writes (see ``data_sources/polygon/equity.py``); an
# unrecognized timeframe sorts last. Keeping the tiebreak deterministic keeps
# the chosen reference reproducible from the archived bars (ALP-747 AC4).
_TIMEFRAME_GRANULARITY_RANK: dict[str, int] = {
    "15min": 0,
    "1h": 1,
    "4h": 2,
    "1d": 3,
    "1w": 4,
}
_UNKNOWN_TIMEFRAME_RANK = 99

# ALP-587 / ALP-747 — staleness ceiling for a recorded bar consumed as a spot
# price. A daily bar is structurally staler than the live-quote freshness window
# (``config.snapshot_freshness_max_price_age_seconds`` is 900s) — applying that
# bound would reject every off-hours reference. A seven-calendar-day ceiling
# clears the longest U.S. market-holiday weekend yet still drops a ticker whose
# price feed has genuinely stalled; a dropped ticker correctly falls back to the
# validation tool's ``UNAVAILABLE`` / ``missing_market_price`` outcome. The
# dispatch-time live-quote coherence check (ALP-747) is the real-time backstop
# for a reference bar that clears this bound but still lags a fast intraday move.
_MAX_REFERENCE_BAR_AGE_SECONDS = 7 * 24 * 60 * 60


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


async def _read_active_universe_prices(
    session: AsyncSession,
    *,
    as_of: datetime,
    max_bar_age_seconds: float = _MAX_REFERENCE_BAR_AGE_SECONDS,
) -> dict[str, float]:
    """Freshest recorded close per active-universe ticker (ALP-587 / ALP-747).

    Reads the most recent :class:`OhlcvBars` row of *any* timeframe for every
    ticker the ``asset_universe`` table marks ``is_active`` and returns
    ``{ticker: unadj_close}``. Pre-ALP-747 this read only ``1d`` bars, so the
    reference price lagged the live intraday price by up to a full session
    during a fast move (the ORCL $204-daily-vs-$226-intraday mispricing); now an
    intraday ``15min`` / ``1h`` / ``4h`` close — which the collector writes on a
    market-hours cadence — supersedes the daily close whenever it is fresher.
    The *unadjusted* close — the actual traded price, not the split-adjusted
    series — is used so the value carries the same semantics as a broker
    ``current_price`` quote. A corporate action between the bar and ``as_of``
    (split, large dividend) leaves the close slightly off as a current-spot
    proxy; that imprecision is accepted for guardrail projection, which is a
    coarse pre-trade notional/margin check rather than a fill-quality gate.

    "Freshest" is the maximum ``period_start`` across all timeframes; on a tie
    (a top-of-hour open shared by a 15min and a 1h bar) the finer-grained bar
    wins (:data:`_TIMEFRAME_GRANULARITY_RANK`), keeping selection deterministic
    and the run reproducible from the archived bars (ALP-747 AC4).

    A ticker whose freshest bar is older than ``max_bar_age_seconds`` is
    dropped: the validation tool's ``UNAVAILABLE`` / ``missing_market_price``
    path is the correct fallback for a price feed that has genuinely
    stalled. The bound is applied in SQL via the ``period_start`` ISO-8601
    string, whose lexicographic order matches chronological order.

    The decision agents propose against the full active universe, of which
    the held book priced by :func:`_build_market_inputs` is a strict
    subset. Held-position quotes still win on overlap — see the merge in
    that function.
    """
    cutoff_iso = (as_of - timedelta(seconds=max_bar_age_seconds)).replace(microsecond=0).isoformat()
    # Rank each in-window bar within its ticker by recency (period_start desc),
    # breaking a period_start tie toward the finer-grained timeframe, then keep
    # the top row per ticker. A row_number window avoids the MAX-per-group
    # self-join's ambiguity when two timeframes share the maximum period_start.
    rank_case = case(
        _TIMEFRAME_GRANULARITY_RANK,
        value=OhlcvBars.timeframe,
        else_=_UNKNOWN_TIMEFRAME_RANK,
    )
    ranked = (
        select(
            OhlcvBars.ticker.label("ticker"),
            OhlcvBars.unadj_close.label("unadj_close"),
            func.row_number()
            .over(
                partition_by=OhlcvBars.ticker,
                order_by=(OhlcvBars.period_start.desc(), rank_case.asc()),
            )
            .label("rn"),
        )
        .join(AssetUniverse, AssetUniverse.ticker == OhlcvBars.ticker)
        .where(
            AssetUniverse.is_active == 1,
            OhlcvBars.period_start >= cutoff_iso,
        )
        .subquery()
    )
    stmt = select(ranked.c.ticker, ranked.c.unadj_close).where(ranked.c.rn == 1)
    rows = (await session.execute(stmt)).all()
    return {str(ticker): float(close) for ticker, close in rows}


def _build_market_inputs(
    *,
    positions: tuple[PositionSnapshot, ...],
    universe_prices: Mapping[str, float],
    risk_free_rate: float,
    as_of: datetime,
    iv_provider: IvProvider,
) -> MarketInputs:
    """Compose ``MarketInputs`` from universe + broker-position prices.

    ``underlying_prices`` merges two layers: ``universe_prices`` — latest
    EOD closes for every active-universe ticker (ALP-587) — forms the base,
    and each held position's ``current_price`` overrides it on overlap (a
    live broker quote beats an EOD bar). The base layer lets the decision
    agents validate proposals against active-universe tickers they do not
    yet hold; without it the validation tool returns ``UNAVAILABLE`` /
    ``missing_market_price`` for any unheld candidate.

    ``iv_provider`` is the caller-constructed IV-sourcing seam (ALP-642 —
    production-side this is :class:`SqlOptionsIvProvider`, resolved against
    ``options_contract_snapshots``). The provider's realized-vol fallback
    map is owned by the caller (``gather_phase1_inputs`` for Phase 1) so
    the same lifecycle that builds the price layers builds the IV layer.
    """
    # ALP-462 — ``pos.current_price`` is ``Price`` (Decimal) on the
    # PositionSnapshot boundary; cast at the legacy MarketInputs surface which
    # still uses float (risk_guardrails/guardrail_evaluation/types is outside ALP-462).
    position_prices: dict[str, float] = {
        pos.symbol: float(pos.current_price) for pos in positions if pos.current_price is not None
    }
    # Held-position live quotes override universe EOD closes on overlap.
    underlying_prices: dict[str, float] = {**universe_prices, **position_prices}
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=risk_free_rate,
        iv_provider=iv_provider,
        as_of=as_of,
    )


async def gather_phase1_inputs(
    *,
    handle: InvocationHandle,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    as_of: datetime,
    sync_session_factory: sessionmaker[Session],
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

    universe_prices = await _read_active_universe_prices(handle.session, as_of=as_of)

    # ALP-642 — wrap the per-underlying realized-vol scalars into the
    # provider's RealizedVolEntry shape once, then hand the SQL-backed
    # production provider down into _build_market_inputs.
    realized_vol_entries: dict[str, RealizedVolEntry] = {
        ticker: RealizedVolEntry(underlying=ticker, trailing_30d_realized_vol=vol)
        for ticker, vol in realized_vol_map.items()
    }
    iv_provider = SqlOptionsIvProvider(
        sync_session_factory=sync_session_factory,
        realized_vol=realized_vol_entries,
    )

    market_inputs = _build_market_inputs(
        positions=positions,
        universe_prices=universe_prices,
        risk_free_rate=risk_free_rate,
        as_of=as_of,
        iv_provider=iv_provider,
    )

    return Phase1Inputs(
        ca_activities=ca_activities,
        alpaca_positions=positions,
        alpaca_account=account,
        market_inputs=market_inputs,
        staleness_flag=staleness_flag,
    )
