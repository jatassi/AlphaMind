"""Production wiring helpers for the breach-loop substrate (ALP-453).

The breach loop's run-forever entry point consumes a fan of injected
factories: ``snapshot_provider``, ``regime_provider``,
``library_config_factory``, the dispatcher's per-tick ``context_provider``,
and ``submit_envelope``. The parent work tree (ALP-123) shipped no-op
stubs so the supervisor could register the task; this module replaces
each stub with substrate backed by the existing SQL + config + OMS
plumbing.

Each helper is a thin closure builder — the actual primitives
(:class:`SqlPortfolioStateRepository`, :func:`assemble_snapshot`,
:func:`to_library_snapshot`, :func:`resolve_regime_adaptation`,
:func:`from_resolved_config`, :func:`submit_engine_envelope`) live in
their owning modules and carry their own test coverage. Wiring tests in
``tests/execution/continuous_monitor/breach_loop/test_production_substrate.py``
pin the closure contracts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.commands.engine_envelope import EngineEnvelope as OmsEngineEnvelope
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.config.models.main import MainConfig
from alphamind.config.models.modes import Mode
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import (
    LoadedConfig,
    ResolvedConfig,
    RuntimeDimensions,
    compose_config,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    BreachDispatchContext,
)
from alphamind.persistence.models import OhlcvBars
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.synthesizer import adapt_ticker_sector_resolver
from alphamind.portfolio_state.freshness import AssembledSnapshot
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
)
from alphamind.portfolio_state.records.positions import resolve_ticker
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    PositionLiquidity,
    PositionRiskReward,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    IvProvider,
    LibraryConfig,
    MarketInputs,
    evaluate_proposals,
    from_resolved_config,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.library_snapshot import to_library_snapshot
from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput
from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationState
from alphamind.state.config import (
    StatePersistenceConfig,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.repository import (
    build_sql_portfolio_state_repository,
)
from alphamind.state.tables.invocations import InvocationRow

if TYPE_CHECKING:
    from alphamind.execution.continuous_monitor.underlying_stream.cache import (
        UnderlyingPriceCache,
    )

__all__ = [
    "AdvProvider",
    "AssembledSnapshotProvider",
    "LibraryConfigFactory",
    "LibrarySnapshotTranslator",
    "load_breach_loop_resolved_config",
    "make_adv_provider",
    "make_assembled_snapshot_provider",
    "make_dispatch_context_provider",
    "make_invocation_id_provider_sync",
    "make_library_config_factory",
    "make_library_snapshot_translator",
    "make_regime_provider",
    "make_snapshot_provider",
    "make_submit_envelope",
]


log = logging.getLogger(__name__)

LibraryConfigFactory = Callable[[ActiveRiskParameterSet], LibraryConfig]

_BOOTSTRAP_SENTINEL = "monitor-bootstrap"


# ---------------------------------------------------------------------------
# Shared config loader — one resolved snapshot per daemon lifetime
# ---------------------------------------------------------------------------


_DEFAULT_RISK_FREE_RATE = 0.045  # mirrors greeks_refresh.wiring default


def load_breach_loop_resolved_config(config_dir: Path) -> ResolvedConfig:
    """Compose a :class:`ResolvedConfig` for the monitor's lifetime.

    The breach loop runs across invocations; resolved config rarely
    changes mid-lifetime (operator restart on amendment is the design).
    We compose once at daemon startup using the active profile / regime /
    mode declared in ``main.yaml`` so the library-config + sector resolver
    + escalation zones are stable across ticks.

    The runtime dimensions match ``main.active_*`` defaults — the monitor
    does not drive regime transitions itself; the scheduler's invocations
    do that and the monitor reads the resulting active risk parameter set.
    """
    from alphamind.config.loaders import (
        load_modes,
        load_overlays,
        load_profiles,
        load_regimes,
        load_run_types,
    )
    from alphamind.config.models.agents import AgentsConfig
    from alphamind.config.models.assets import AssetsConfig
    from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
    from alphamind.config.models.digest import DigestConfig
    from alphamind.config.models.execution import ExecutionConfig
    from alphamind.config.models.guardrails import GuardrailsConfig
    from alphamind.config.models.llm_failure import LLMFailureConfig
    from alphamind.config.models.scheduler import SchedulerConfig
    from alphamind.config.models.venue import VenueConfig

    main = MainConfig.model_validate(read_yaml_file(config_dir / "main.yaml"))
    loaded = LoadedConfig(
        main=main,
        scheduler=SchedulerConfig.model_validate(read_yaml_file(config_dir / "scheduler.yaml")),
        venue=VenueConfig.model_validate(read_yaml_file(config_dir / "venue.yaml")),
        execution=ExecutionConfig.model_validate(read_yaml_file(config_dir / "execution.yaml")),
        guardrails=GuardrailsConfig.model_validate(read_yaml_file(config_dir / "guardrails.yaml")),
        llm_failure=LLMFailureConfig.model_validate(
            read_yaml_file(config_dir / "llm_failure.yaml")
        ),
        digest=DigestConfig.model_validate(read_yaml_file(config_dir / "digest.yaml")),
        assets=AssetsConfig.model_validate(read_yaml_file(config_dir / "assets.yaml")),
        agents=AgentsConfig.model_validate(read_yaml_file(config_dir / "agents.yaml")),
        continuous_monitor=ContinuousMonitorConfig.model_validate(
            read_yaml_file(config_dir / "continuous_monitor.yaml")
        ),
        profiles=load_profiles(config_dir),
        regimes=load_regimes(config_dir),
        modes=load_modes(config_dir),
        overlays=load_overlays(config_dir),
        run_types=load_run_types(config_dir),
    )
    # The monitor does not drive regime transitions; the scheduler does. We
    # compose against the conservative defaults (``normal`` regime, no
    # overlays) at daemon startup so the LibraryConfig base is stable.
    # ``library_config_factory`` later overrides ``effective_limits`` per
    # tick with the active risk parameter set the most-recent invocation
    # committed, so the regime fold here only seeds the escalation_zones /
    # feature_flags / active_sectors that the daemon-wide config reads.
    runtime = RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.market_hours_rolling,
    )
    return compose_config(loaded, runtime)


# ---------------------------------------------------------------------------
# library_config_factory
# ---------------------------------------------------------------------------


def make_library_config_factory(*, resolved: ResolvedConfig) -> LibraryConfigFactory:
    """Return an :class:`ActiveRiskParameterSet`-keyed :class:`LibraryConfig` factory.

    Strategy: build a base :class:`LibraryConfig` once via
    :func:`from_resolved_config` (so escalation_zones, feature_flags,
    active_sectors are stable across ticks), then on each call override
    ``effective_limits`` with the per-tick values from the supplied
    :class:`ActiveRiskParameterSet`.

    The set's entries are the canonical per-tick limit values — they
    already carry the regime-fold / overlay-fold the scheduler's
    invocation produced. Overriding here keeps the breach loop's
    classification math against the limits the scheduler committed to,
    even when the monitor's resolved config drifts.
    """
    base = from_resolved_config(resolved)

    def _factory(active: ActiveRiskParameterSet) -> LibraryConfig:
        per_tick_limits = MappingProxyType(
            {entry.rule_id: float(entry.value) for entry in active.entries}
        )
        return LibraryConfig(
            effective_limits=per_tick_limits,
            escalation_zones=base.escalation_zones,
            feature_flags=base.feature_flags,
            active_sectors=base.active_sectors,
            active_regime=base.active_regime,
            active_profile=base.active_profile,
            conservative_buffer_pct=base.conservative_buffer_pct,
        )

    return _factory


# ---------------------------------------------------------------------------
# invocation_id provider (sync) — used by the SQL repository's
# active-risk-parameters provider and the submit_envelope handle factory
# ---------------------------------------------------------------------------


async def _read_latest_invocation_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str] | None:
    """Read ``(invocation_id, resolved_config_snapshot_path, active_regime)``
    for the most-recently-started invocation, or ``None`` on a fresh DB.

    Combining the three columns in one SELECT lets snapshot and regime
    providers share a single row read per tick instead of issuing two
    near-identical ``ORDER BY start_at DESC LIMIT 1`` queries.
    """
    async with session_factory() as sess:
        stmt = (
            select(
                InvocationRow.invocation_id,
                InvocationRow.resolved_config_snapshot_path,
                InvocationRow.active_regime,
            )
            .order_by(InvocationRow.start_at.desc())
            .limit(1)
        )
        result = await sess.execute(stmt)
        row = result.one_or_none()
    if row is None:
        return None
    invocation_id, snapshot_path, active_regime = row
    return str(invocation_id), str(snapshot_path), str(active_regime)


def make_invocation_id_provider_sync(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[str]]:
    """Mirror :func:`greeks_refresh.wiring.make_invocation_id_provider`.

    Async — the breach loop's call site runs inside the supervisor's loop.
    """

    async def _provider() -> str:
        row = await _read_latest_invocation_row(session_factory)
        return _BOOTSTRAP_SENTINEL if row is None else row[0]

    return _provider


# ---------------------------------------------------------------------------
# active_risk_parameters provider — derives the per-tick set from the most
# recent invocation's resolved-config snapshot.
# ---------------------------------------------------------------------------


_REGIME_TO_LABEL: dict[Regime, RegimeLabel] = {
    Regime.low_vol: RegimeLabel.LOW_VOL,
    Regime.normal: RegimeLabel.NORMAL,
    Regime.elevated: RegimeLabel.ELEVATED,
    Regime.crisis: RegimeLabel.CRISIS,
}


def _build_active_risk_parameters(
    *,
    rule_values: Mapping[str, float],
    regime: Regime,
) -> ActiveRiskParameterSet:
    """Mirror :func:`scheduler.orchestrator._build_active_risk_parameters`.

    Wraps the resolver's already-folded ``rule_values`` so the breach loop
    consumes the same parameter set the most recent invocation committed.
    """
    entries = tuple(
        ActiveRiskParameterEntry(
            rule_id=rule_id,
            rule_label=rule_id,
            value=float(value),
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=float(value),
        )
        for rule_id, value in sorted(rule_values.items())
    )
    return ActiveRiskParameterSet(
        regime_label=_REGIME_TO_LABEL[regime],
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


async def _load_active_risk_parameters_from_row(
    row: tuple[str, str, str] | None,
    *,
    fallback: ActiveRiskParameterSet,
) -> ActiveRiskParameterSet:
    """Build the active parameter set from the latest invocation row.

    ``row`` is the tuple :func:`_read_latest_invocation_row` returns. On
    fresh DB / missing snapshot / read error / unknown regime, log a warning
    and return ``fallback`` so the breach loop can tick during bootstrap
    rather than crashing.
    """
    import asyncio
    import json

    if row is None:
        # Fresh-DB case — no invocations yet. Common during bootstrap.
        return fallback
    _, snapshot_path, active_regime_str = row
    try:
        contents = await asyncio.to_thread(Path(snapshot_path).read_text)
        payload = json.loads(contents)
    except FileNotFoundError:
        log.warning(
            "active_risk_parameters fallback: snapshot file missing path=%s; "
            "using bootstrap parameters",
            snapshot_path,
        )
        return fallback
    except (OSError, ValueError):
        log.warning(
            "active_risk_parameters fallback: failed to read or parse snapshot path=%s; "
            "using bootstrap parameters",
            snapshot_path,
            exc_info=True,
        )
        return fallback
    rule_values = payload.get("rule_values")
    if not isinstance(rule_values, dict):
        log.warning(
            "active_risk_parameters fallback: snapshot %s has no 'rule_values' dict; "
            "using bootstrap parameters",
            snapshot_path,
        )
        return fallback
    try:
        regime = Regime(active_regime_str)
    except ValueError:
        log.warning(
            "active_risk_parameters fallback: invocation active_regime=%r is not a known "
            "Regime value; using bootstrap parameters",
            active_regime_str,
        )
        return fallback
    return _build_active_risk_parameters(
        rule_values={k: float(v) for k, v in rule_values.items()},
        regime=regime,
    )


# ---------------------------------------------------------------------------
# Inner assembly helper — underlies make_assembled_snapshot_provider
# ---------------------------------------------------------------------------


async def _assemble_for_breach_loop_tick(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    state_persistence_config: StatePersistenceConfig,
    underlying_cache: UnderlyingPriceCache,
    portfolio_state_config: PortfolioStateConfig,
    bootstrap_parameters: ActiveRiskParameterSet,
    position_sector_resolver: SectorResolver,
    invocation_row: tuple[str, str, str] | None,
    as_of: datetime,
) -> AssembledSnapshot:
    """Run the assemble pipeline once for the supplied invocation row.

    Underlies :func:`make_assembled_snapshot_provider`; the row read,
    parameter-set hydration, repository construction, and
    :func:`assemble_snapshot` call live here so the provider is a thin
    closure over them. Callers own the upstream ``_read_latest_invocation_row``
    so each can apply its own bootstrap policy before invoking.
    """
    invocation_id = _BOOTSTRAP_SENTINEL if invocation_row is None else invocation_row[0]
    active = await _load_active_risk_parameters_from_row(
        invocation_row, fallback=bootstrap_parameters
    )

    def _active_provider() -> ActiveRiskParameterSet:
        return active

    def _prior_provider(_snapshot_path: str) -> ActiveRiskParameterSet:
        return active

    repository = build_sql_portfolio_state_repository(
        session_factory=session_factory,
        invocation_id=invocation_id,
        active_risk_parameters_provider=_active_provider,
        prior_active_risk_parameters_provider=_prior_provider,
        config=state_persistence_config,
    )
    price_provider = _build_price_provider(underlying_cache, as_of=as_of)
    return assemble_snapshot(
        repository=repository,
        price_provider=price_provider,
        sector_resolver=position_sector_resolver,
        config=portfolio_state_config,
        now=as_of,
    )


# ---------------------------------------------------------------------------
# assembled_snapshot_provider
# ---------------------------------------------------------------------------


AssembledSnapshotProvider = Callable[[], Awaitable[AssembledSnapshot]]


def make_assembled_snapshot_provider(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    underlying_cache: UnderlyingPriceCache,
    resolved: ResolvedConfig,
    portfolio_state_config: PortfolioStateConfig,
    state_persistence_config: StatePersistenceConfig,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AssembledSnapshotProvider:
    """Build the substrate's per-call :class:`AssembledSnapshot` provider.

    The breach loop's :class:`LibrarySnapshot` provider and the cascade
    dispatcher's per-immediate-breach context provider both share one
    instance of this provider so the assemble pipeline (latest-invocation
    SELECT + active-parameter read + SQL repository composition + price
    projection + :func:`assemble_snapshot`) runs at most once per call
    rather than once per consumer (ALP-510).

    Bootstrap contract: on a fresh DB with no invocation rows, the inner
    pipeline runs against ``_BOOTSTRAP_SENTINEL`` and returns an empty
    snapshot — the same path :func:`make_snapshot_provider` exercised
    before ALP-510. The cascade dispatcher only fires on immediate-breach
    events, which presuppose a running breach loop with a seeded
    ``cash_ledger`` singleton, so this branch is unreachable in production.
    """
    bootstrap_parameters = _build_active_risk_parameters(
        rule_values=resolved.rule_values,
        regime=Regime(resolved.regime_label),
    )
    ticker_sector_resolver = _build_sector_resolver(resolved)
    position_sector_resolver = adapt_ticker_sector_resolver(ticker_sector_resolver)

    async def _provider() -> AssembledSnapshot:
        row = await _read_latest_invocation_row(session_factory)
        return await _assemble_for_breach_loop_tick(
            session_factory=session_factory,
            state_persistence_config=state_persistence_config,
            underlying_cache=underlying_cache,
            portfolio_state_config=portfolio_state_config,
            bootstrap_parameters=bootstrap_parameters,
            position_sector_resolver=position_sector_resolver,
            invocation_row=row,
            as_of=now(),
        )

    return _provider


# ---------------------------------------------------------------------------
# library_snapshot_translator
# ---------------------------------------------------------------------------


LibrarySnapshotTranslator = Callable[
    [PortfolioStateSnapshot],
    LibrarySnapshot,
]


def make_library_snapshot_translator(
    *,
    resolved: ResolvedConfig,
) -> LibrarySnapshotTranslator:
    """Return a pure :class:`PortfolioStateSnapshot` → :class:`LibrarySnapshot` adapter.

    Hides the sector-resolver wiring behind a single callable both the
    breach-loop snapshot provider and cascade-dispatch context provider
    consume.
    """
    ticker_sector_resolver = _build_sector_resolver(resolved)

    def _translate(snapshot: PortfolioStateSnapshot) -> LibrarySnapshot:
        return to_library_snapshot(snapshot, sector_resolver=ticker_sector_resolver)

    return _translate


# ---------------------------------------------------------------------------
# snapshot_provider
# ---------------------------------------------------------------------------


SnapshotProvider = Callable[[], Awaitable[LibrarySnapshot]]


def make_snapshot_provider(
    *,
    assembled_snapshot_provider: AssembledSnapshotProvider,
    library_snapshot_translator: LibrarySnapshotTranslator,
) -> SnapshotProvider:
    """Build the breach-loop's per-tick :class:`LibrarySnapshot` provider.

    Composes :func:`make_assembled_snapshot_provider` and
    :func:`make_library_snapshot_translator` so the breach loop sees the
    same :class:`AssembledSnapshot` the cascade dispatcher consumes, with
    one shared sector-resolution wiring.
    """

    async def _provider() -> LibrarySnapshot:
        assembled = await assembled_snapshot_provider()
        return library_snapshot_translator(assembled.snapshot)

    return _provider


def _build_price_provider(
    underlying_cache: UnderlyingPriceCache, *, as_of: datetime
) -> StubCurrentPriceProvider:
    """Project the live underlying-cache into the canonical price-provider shape."""
    quotes = {
        ticker: PriceQuote(
            ticker=ticker,
            price_usd=quote.price,
            as_of_timestamp=as_of,
            source=PriceSource.INTRADAY_QUOTE,
            is_stale=False,
        )
        for ticker, quote in underlying_cache.get_all().items()
    }
    return StubCurrentPriceProvider(quotes, as_of)


def _build_sector_resolver(resolved: ResolvedConfig) -> Callable[[str], str]:
    """Build a ticker→sector resolver from the resolved assets config.

    Mirrors :func:`scheduler.orchestrator._build_sector_resolver`.
    """
    ticker_to_sector: dict[str, str] = {}
    if hasattr(resolved, "assets") and hasattr(resolved.assets, "sectors"):
        for sector, tickers in resolved.assets.sectors.items():
            for ticker in tickers:
                ticker_to_sector[ticker] = sector

    def _resolver(ticker: str) -> str:
        return ticker_to_sector.get(ticker, "UNCLASSIFIED")

    return _resolver


# ---------------------------------------------------------------------------
# regime_provider
# ---------------------------------------------------------------------------


RegimeProvider = Callable[[], Awaitable[RegimeAdaptationOutput]]


def make_regime_provider(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    resolved: ResolvedConfig,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RegimeProvider:
    """Build the per-tick :class:`RegimeAdaptationOutput` provider.

    Mirrors :func:`scheduler.orchestrator._build_synthetic_regime_output`:
    the monitor process does not run the full regime-adaptation pipeline
    (that lives in the scheduler's invocation path); instead it reads the
    most recent invocation's already-resolved parameter set and wraps it
    in a synthetic :class:`RegimeAdaptationOutput` so the breach loop's
    :func:`compose_phase_1_enforcement` call site receives the canonical
    shape. When the regime-adaptation orchestrator is threaded through the
    monitor (deferred follow-up), this synthetic shim retires.
    """
    bootstrap_parameters = _build_active_risk_parameters(
        rule_values=resolved.rule_values,
        regime=Regime(resolved.regime_label),
    )
    fallback_regime = Regime(resolved.regime_label)

    async def _provider() -> RegimeAdaptationOutput:
        row = await _read_latest_invocation_row(session_factory)
        invocation_id = _BOOTSTRAP_SENTINEL if row is None else row[0]
        active = await _load_active_risk_parameters_from_row(row, fallback=bootstrap_parameters)
        regime_for_state = _resolve_regime_from_label(active.regime_label, fallback_regime)
        state = RegimeAdaptationState(
            as_of=now().isoformat().replace("+00:00", "Z"),
            invocation_id=invocation_id,
            active_regime=regime_for_state,
            prior_regime=None,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            transition_started_invocation_id=None,
            transition_origin_regime=None,
            active_overlays=(),
            distillation_regime_label=regime_for_state.value,
            distillation_vix_level=0.0,
            regime_skip_emergency=False,
        )
        return RegimeAdaptationOutput(
            runtime_dimensions_active_regime=regime_for_state,
            runtime_dimensions_active_overlays=(),
            overlay_activation_decisions=(),
            effective_limits={},
            active_risk_parameter_set=active,
            regime_transition_breaches=(),
            regime_skip_emergency=False,
            new_persisted_state=state,
            audit_log_entries=(),
        )

    return _provider


_LABEL_TO_REGIME: dict[RegimeLabel, Regime] = {
    RegimeLabel.LOW_VOL: Regime.low_vol,
    RegimeLabel.NORMAL: Regime.normal,
    RegimeLabel.ELEVATED: Regime.elevated,
    RegimeLabel.CRISIS: Regime.crisis,
}


def _resolve_regime_from_label(
    label: RegimeLabel,
    fallback: Regime,
) -> Regime:
    """Map a :class:`RegimeLabel` back to the config :class:`Regime` enum."""
    return _LABEL_TO_REGIME.get(label, fallback)


# ---------------------------------------------------------------------------
# ADV provider — per-symbol 20-day average daily volume from OhlcvBars
# ---------------------------------------------------------------------------


AdvProvider = Callable[[], Awaitable[Mapping[str, float]]]

_TIMEFRAME_DAILY = "1d"
_DEFAULT_ADV_LOOKBACK_DAYS = 20


def make_adv_provider(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    lookback_days: int = _DEFAULT_ADV_LOOKBACK_DAYS,
) -> AdvProvider:
    """Return a provider of per-ticker ADV (average daily volume in shares).

    Averages up to the most recent ``lookback_days`` daily ``adj_volume``
    rows from :class:`OhlcvBars` (timeframe ``"1d"``) per ticker — tickers
    with fewer than ``lookback_days`` bars in the table contribute the
    average of what is available, rather than being excluded. The cascade
    dispatcher uses the returned mapping to derive each open position's
    ``adv_to_position_size_ratio`` (= ``adv_shares * underlying_price /
    position_notional_usd``) — higher means more liquid relative to size.

    The provider is awaited fresh per immediate breach so the dispatcher
    sees newly-ingested daily bars without a daemon restart. Daily bars
    refresh once per session, so the SELECT cost is amortized across the
    rare immediate-breach path.
    """

    async def _provider() -> Mapping[str, float]:
        async with session_factory() as sess:
            row_number = (
                func.row_number()
                .over(
                    partition_by=OhlcvBars.ticker,
                    order_by=OhlcvBars.period_start.desc(),
                )
                .label("rn")
            )
            ranked = (
                select(
                    OhlcvBars.ticker,
                    OhlcvBars.adj_volume,
                    row_number,
                )
                .where(OhlcvBars.timeframe == _TIMEFRAME_DAILY)
                .subquery()
            )
            stmt = (
                select(
                    ranked.c.ticker,
                    func.avg(ranked.c.adj_volume).label("adv"),
                )
                .where(ranked.c.rn <= lookback_days)
                .group_by(ranked.c.ticker)
            )
            result = await sess.execute(stmt)
            return {str(ticker): float(adv) for ticker, adv in result.all() if adv is not None}

    return _provider


# ---------------------------------------------------------------------------
# context_provider — builds BreachDispatchContext per dispatch
# ---------------------------------------------------------------------------


DispatchContextProvider = Callable[[], Awaitable[BreachDispatchContext]]


def _compute_adv_to_position_size_ratio(
    position: PositionView,
    *,
    adv_shares_by_ticker: Mapping[str, float],
    underlying_prices: Mapping[str, float],
) -> float:
    """Return ``(ADV_shares * underlying_price) / position_notional_usd``.

    Falls back to ``0.0`` (lowest liquidity, treated as worst-tiebreaker)
    when any input is missing or zero: the underlying isn't priced on the
    live cache, the ADV provider has no row for the ticker, or the
    position's notional is zero. The cascade selectors require an entry
    per open position, so the fallback keeps the contract intact rather
    than excluding the position from selection.
    """
    ticker = resolve_ticker(position.details)
    if ticker is None:
        return 0.0
    adv_shares = adv_shares_by_ticker.get(ticker)
    if adv_shares is None or adv_shares <= 0.0:
        return 0.0
    price = underlying_prices.get(ticker)
    if price is None or price <= 0.0:
        return 0.0
    notional = float(position.notional_exposure_usd)
    if notional <= 0.0:
        return 0.0
    return (adv_shares * price) / notional


def make_dispatch_context_provider(  # noqa: PLR0913 — composition root; each parameter is one production-substrate seam
    *,
    assembled_snapshot_provider: AssembledSnapshotProvider,
    library_snapshot_translator: LibrarySnapshotTranslator,
    regime_provider: RegimeProvider,
    library_config_factory: LibraryConfigFactory,
    underlying_cache: UnderlyingPriceCache,
    iv_provider: IvProvider,
    adv_provider: AdvProvider,
    progressive_tiers: tuple[ProgressiveTier, ...] = (),
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DispatchContextProvider:
    """Return an async context provider the cascade dispatcher awaits per immediate breach.

    The caller threads ``iv_provider`` — the same instance the breach-loop
    task itself reads — so cascade re-projections and breach-loop
    evaluations see identical IV lookups. ``adv_provider`` returns the
    per-ticker 20-day average daily volume (shares); the provider combines
    it with live underlying prices and each position's notional to derive
    ``PositionLiquidity.adv_to_position_size_ratio``. ``PositionRiskReward``
    is read directly from :attr:`PositionView.risk_reward_at_current` — None
    on positions without a bracket falls back to ``0.0``, which the
    margin-call selector treats as worse than any positive R/R but better
    than the pathological negative R/R of a position past invalidation.

    The closure awaits ``assembled_snapshot_provider`` once and derives both
    the library-shape snapshot (via ``library_snapshot_translator``) and
    the open-position tuple from the same :class:`AssembledSnapshot`, so
    the assemble pipeline runs at most once per immediate-breach event
    (ALP-510).

    ``progressive_tiers`` flows into :class:`BreachDispatchContext` so the
    cascade orchestrator's follow-up loop has the cumulative-drawdown tier
    sequence (driven by ``GuardrailsConfig.rules`` at daemon startup).
    """

    async def _provider() -> BreachDispatchContext:
        (
            assembled,
            regime_output,
            adv_shares_by_ticker,
        ) = await asyncio.gather(
            assembled_snapshot_provider(),
            regime_provider(),
            adv_provider(),
        )
        library_snapshot = library_snapshot_translator(assembled.snapshot)
        open_positions = assembled.snapshot.open_positions
        library_config = library_config_factory(regime_output.active_risk_parameter_set)
        underlying_prices = {t: q.price for t, q in underlying_cache.get_all().items()}
        market_inputs = MarketInputs(
            underlying_prices=underlying_prices,
            risk_free_rate=risk_free_rate,
            iv_provider=iv_provider,
            as_of=now(),
        )
        liquidity = tuple(
            PositionLiquidity(
                position_id=p.position_id,
                adv_to_position_size_ratio=_compute_adv_to_position_size_ratio(
                    p,
                    adv_shares_by_ticker=adv_shares_by_ticker,
                    underlying_prices=underlying_prices,
                ),
            )
            for p in open_positions
        )
        risk_reward = tuple(
            PositionRiskReward(
                position_id=p.position_id,
                risk_reward_ratio=(
                    p.risk_reward_at_current if p.risk_reward_at_current is not None else 0.0
                ),
            )
            for p in open_positions
        )
        return BreachDispatchContext(
            open_positions=open_positions,
            liquidity=liquidity,
            risk_reward_metric=risk_reward,
            library_snapshot=library_snapshot,
            library_config=library_config,
            market_inputs=market_inputs,
            evaluate_proposals=cast(Any, evaluate_proposals),
            portfolio_value_usd=library_snapshot.portfolio_value_usd,
            # ``portfolio_state.records.capital.RegimeLabel`` and
            # ``breach_behavior.RegimeLabel`` are the same enum object (both
            # re-export ``regime_adaptation.types.RegimeLabel``) — no conversion
            # needed at the type or value level.
            active_regime=regime_output.active_risk_parameter_set.regime_label,
            progressive_tiers=progressive_tiers,
        )

    return _provider


# ---------------------------------------------------------------------------
# submit_envelope — wraps submit_engine_envelope with InvocationHandle
# ---------------------------------------------------------------------------


SubmitEnvelope = Callable[[OmsEngineEnvelope], Awaitable[Any]]


def make_submit_envelope(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    monitor_session_id: str,
    state_persistence_config: StatePersistenceConfig,
    invocation_id_provider: Callable[[], Awaitable[str]] | None = None,
) -> SubmitEnvelope:
    """Return the cascade dispatcher's ``submit_envelope`` callable.

    Per-call:
    1. Resolve the latest invocation_id (so the activity-log FK holds).
    2. Open a fresh :class:`AsyncSession`; build an :class:`InvocationHandle`.
    3. Invoke :func:`submit_engine_envelope` against the handle.
    4. Commit the session on success; rollback on failure.

    The submit-state cell (the dedup set of trigger_ids) is created once
    per monitor session via :func:`build_initial_submit_engine_envelope_state`
    so duplicate-trigger protection holds across all envelopes the monitor
    submits within one session.
    """
    # After ALP-458 the decision↔execution cycle is gone and the
    # ``__getattr__`` lazy-loader has been deleted, so a direct import
    # works at module-load time.
    from alphamind.execution.oms.submit_engine_envelope import (
        build_initial_submit_engine_envelope_state,
        submit_engine_envelope,
    )

    state = build_initial_submit_engine_envelope_state(monitor_session_id=monitor_session_id)
    id_provider = invocation_id_provider or make_invocation_id_provider_sync(session_factory)

    async def _submit(envelope: OmsEngineEnvelope) -> Any:
        nonlocal state
        invocation_id = await id_provider()
        async with session_factory() as session:
            handle = InvocationHandle(session=session, invocation_id=invocation_id)
            # ALP-476 — submit_engine_envelope returns ``(result, new_state)``;
            # rebind the closure cell so the dedup frozenset persists across
            # subsequent envelopes within the same monitor session.
            result, state = await submit_engine_envelope(
                envelope,
                handle=handle,
                state=state,
                config=state_persistence_config,
            )
            await session.commit()
            return result

    return _submit
