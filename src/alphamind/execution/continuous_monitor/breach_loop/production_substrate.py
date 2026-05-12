"""Production wiring helpers for the breach-loop substrate (ALP-453).

The breach loop's run-forever entry point consumes a fan of injected
factories: ``snapshot_provider``, ``regime_provider``,
``library_config_factory``, the dispatcher's per-tick ``context_provider``,
and ``submit_envelope``. The work-tree work-tree (ALP-123) shipped no-op
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

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.loaders import read_yaml_file
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
from alphamind.execution.oms.engine_envelope import EngineEnvelope as OmsEngineEnvelope
from alphamind.execution.state_persistence.config import (
    StatePersistenceConfig,
    load_state_persistence_config,
)
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.repository import (
    build_sql_portfolio_state_repository,
)
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    RegimeTransitionState,
)
from alphamind.portfolio_state.records.capital import (
    RegimeLabel as PortfolioRegimeLabel,
)
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.consumers.synthesizer import adapt_ticker_sector_resolver
from alphamind.portfolio_state.library_snapshot import to_library_snapshot
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
)
from alphamind.risk_guardrails.breach_behavior import (
    PositionLiquidity,
    PositionRiskReward,
)
from alphamind.risk_guardrails.breach_behavior import (
    RegimeLabel as BreachRegimeLabel,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    LibraryConfig,
    MarketInputs,
    evaluate_proposals,
    from_resolved_config,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput
from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationState

if TYPE_CHECKING:
    from alphamind.execution.continuous_monitor.underlying_stream.cache import (
        UnderlyingPriceCache,
    )

__all__ = [
    "LibraryConfigFactory",
    "load_breach_loop_resolved_config",
    "make_dispatch_context_provider",
    "make_invocation_id_provider_sync",
    "make_library_config_factory",
    "make_regime_provider",
    "make_snapshot_provider",
    "make_submit_envelope",
]


LibraryConfigFactory = Callable[[ActiveRiskParameterSet], LibraryConfig]


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


def make_library_config_factory(*, config_dir: Path) -> LibraryConfigFactory:
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
    resolved = load_breach_loop_resolved_config(config_dir)
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


async def _read_latest_invocation_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Resolve the most-recently-started invocation_id; fall back to the bootstrap sentinel."""
    async with session_factory() as sess:
        stmt = select(InvocationRow.invocation_id).order_by(InvocationRow.start_at.desc()).limit(1)
        result = await sess.execute(stmt)
        value = result.scalar_one_or_none()
    return "monitor-bootstrap" if value is None else str(value)


def make_invocation_id_provider_sync(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[str]]:
    """Mirror :func:`greeks_refresh.wiring.make_invocation_id_provider`.

    Async — the breach loop's call site runs inside the supervisor's loop.
    """

    async def _provider() -> str:
        return await _read_latest_invocation_id(session_factory)

    return _provider


# ---------------------------------------------------------------------------
# active_risk_parameters provider — derives the per-tick set from the most
# recent invocation's resolved-config snapshot.
# ---------------------------------------------------------------------------


_REGIME_TO_LABEL: dict[Regime, PortfolioRegimeLabel] = {
    Regime.low_vol: PortfolioRegimeLabel.LOW_VOL,
    Regime.normal: PortfolioRegimeLabel.NORMAL,
    Regime.elevated: PortfolioRegimeLabel.ELEVATED,
    Regime.crisis: PortfolioRegimeLabel.CRISIS,
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


async def _load_active_risk_parameters_from_invocation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fallback: ActiveRiskParameterSet,
) -> ActiveRiskParameterSet:
    """Read the latest invocation row, load its resolved-config snapshot, build a set.

    On fresh DB / missing snapshot / read error, return ``fallback`` so the
    breach loop can tick during bootstrap rather than crashing.
    """
    import asyncio
    import json

    async with session_factory() as sess:
        stmt = (
            select(InvocationRow.resolved_config_snapshot_path, InvocationRow.active_regime)
            .order_by(InvocationRow.start_at.desc())
            .limit(1)
        )
        row = (await sess.execute(stmt)).one_or_none()
    if row is None:
        return fallback
    snapshot_path, active_regime_str = row
    try:
        contents = await asyncio.to_thread(Path(snapshot_path).read_text)
        payload = json.loads(contents)
    except (FileNotFoundError, OSError, ValueError):
        return fallback
    rule_values = payload.get("rule_values")
    if not isinstance(rule_values, dict):
        return fallback
    try:
        regime = Regime(active_regime_str)
    except ValueError:
        return fallback
    return _build_active_risk_parameters(
        rule_values={k: float(v) for k, v in rule_values.items()},
        regime=regime,
    )


# ---------------------------------------------------------------------------
# snapshot_provider
# ---------------------------------------------------------------------------


SnapshotProvider = Callable[[], Awaitable[LibrarySnapshot]]


def make_snapshot_provider(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    underlying_cache: UnderlyingPriceCache,
    config_dir: Path,
    state_persistence_config: StatePersistenceConfig | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SnapshotProvider:
    """Build the breach-loop's per-tick :class:`LibrarySnapshot` provider.

    Each call resolves the most recent invocation_id, constructs a
    :class:`SqlPortfolioStateRepository` scoped to it, drives
    :func:`assemble_snapshot` against current underlying prices from the
    monitor's :class:`UnderlyingPriceCache`, and translates the result via
    :func:`to_library_snapshot`. The pricing provider is built per call
    from the live cache so the snapshot reads the freshest spots.
    """
    resolved = load_breach_loop_resolved_config(config_dir)
    portfolio_state_config = load_portfolio_state_config(config_dir / "portfolio_state.yaml")
    persistence_config = state_persistence_config or load_state_persistence_config(
        read_yaml_file(config_dir / "main.yaml")
    )
    bootstrap_parameters = _build_active_risk_parameters(
        rule_values=resolved.rule_values,
        regime=Regime(resolved.regime_label),
    )
    ticker_sector_resolver = _build_sector_resolver(resolved)
    position_sector_resolver = adapt_ticker_sector_resolver(ticker_sector_resolver)

    async def _provider() -> LibrarySnapshot:
        invocation_id = await _read_latest_invocation_id(session_factory)
        active = await _load_active_risk_parameters_from_invocation(
            session_factory, fallback=bootstrap_parameters
        )

        async def _active_provider() -> ActiveRiskParameterSet:
            return active

        async def _prior_provider(_snapshot_path: str) -> ActiveRiskParameterSet:
            return active

        repository = build_sql_portfolio_state_repository(
            session_factory=session_factory,
            invocation_id=invocation_id,
            active_risk_parameters_provider=_active_provider,
            prior_active_risk_parameters_provider=_prior_provider,
            config=persistence_config,
        )
        price_provider = _build_price_provider(underlying_cache, as_of=now())
        assembled = await assemble_snapshot(
            repository=repository,
            price_provider=price_provider,
            sector_resolver=position_sector_resolver,
            config=portfolio_state_config,
            now=now(),
        )
        return to_library_snapshot(
            assembled.snapshot,
            sector_resolver=ticker_sector_resolver,
        )

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
    config_dir: Path,
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
    resolved = load_breach_loop_resolved_config(config_dir)
    bootstrap_parameters = _build_active_risk_parameters(
        rule_values=resolved.rule_values,
        regime=Regime(resolved.regime_label),
    )

    async def _provider() -> RegimeAdaptationOutput:
        invocation_id = await _read_latest_invocation_id(session_factory)
        active = await _load_active_risk_parameters_from_invocation(
            session_factory, fallback=bootstrap_parameters
        )
        regime_for_state = _resolve_regime_from_label(
            active.regime_label, Regime(resolved.regime_label)
        )
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


_LABEL_TO_REGIME: dict[PortfolioRegimeLabel, Regime] = {
    PortfolioRegimeLabel.LOW_VOL: Regime.low_vol,
    PortfolioRegimeLabel.NORMAL: Regime.normal,
    PortfolioRegimeLabel.ELEVATED: Regime.elevated,
    PortfolioRegimeLabel.CRISIS: Regime.crisis,
}


def _resolve_regime_from_label(
    label: PortfolioRegimeLabel,
    fallback: Regime,
) -> Regime:
    """Map a :class:`RegimeLabel` back to the config :class:`Regime` enum."""
    return _LABEL_TO_REGIME.get(label, fallback)


# ---------------------------------------------------------------------------
# context_provider — builds BreachDispatchContext per dispatch
# ---------------------------------------------------------------------------


DispatchContextProvider = Callable[[], Awaitable[BreachDispatchContext]]


def make_dispatch_context_provider(
    *,
    snapshot_provider: SnapshotProvider,
    regime_provider: RegimeProvider,
    library_config_factory: LibraryConfigFactory,
    underlying_cache: UnderlyingPriceCache,
    open_positions_reader: Callable[[], Awaitable[tuple[Any, ...]]] | None = None,
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DispatchContextProvider:
    """Return an async context provider the cascade dispatcher awaits per immediate breach.

    The dispatcher (post-ALP-453) awaits a sync-or-async context provider.
    Production wiring is async because every read (SQL snapshot, regime,
    library config) is async. The breach loop calls
    ``await on_immediate_breach(...)`` which awaits
    ``dispatcher.handle_immediate_breach`` which awaits this provider.

    Liquidity defaults to a constant ADV ratio (``10.0``) and risk/reward
    defaults to ``2.0`` per position — the live ADV / R/R signals do not
    yet flow into the monitor process, so the dispatcher's secondary-breach
    arithmetic uses the same conservative defaults the verify script uses.
    Replacing these with live signals is a follow-up once the monitor's
    market-data path exposes them.

    ``open_positions_reader`` is an optional reader returning
    ``tuple[PositionView, ...]`` for the per-tick context. When omitted,
    the context's ``open_positions`` is empty and the dispatcher relies on
    its per-rule kwargs providers to surface the breaching position.
    """

    async def _provider() -> BreachDispatchContext:
        library_snapshot = await snapshot_provider()
        regime_output = await regime_provider()
        library_config = library_config_factory(regime_output.active_risk_parameter_set)
        market_inputs = MarketInputs(
            underlying_prices={t: q.price for t, q in underlying_cache.get_all().items()},
            risk_free_rate=risk_free_rate,
            iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
            as_of=now(),
        )
        open_positions: tuple[Any, ...] = ()
        if open_positions_reader is not None:
            open_positions = await open_positions_reader()
        liquidity = tuple(
            PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=10.0)
            for p in open_positions
        )
        risk_reward = tuple(
            PositionRiskReward(position_id=p.position_id, risk_reward_ratio=2.0)
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
            active_regime=_portfolio_to_breach_label(
                regime_output.active_risk_parameter_set.regime_label
            ),
        )

    return _provider


_BREACH_LABEL_MAP: dict[PortfolioRegimeLabel, BreachRegimeLabel] = {
    PortfolioRegimeLabel.LOW_VOL: BreachRegimeLabel.LOW_VOL,
    PortfolioRegimeLabel.NORMAL: BreachRegimeLabel.NORMAL,
    PortfolioRegimeLabel.ELEVATED: BreachRegimeLabel.ELEVATED,
    PortfolioRegimeLabel.CRISIS: BreachRegimeLabel.CRISIS,
}


def _portfolio_to_breach_label(label: PortfolioRegimeLabel) -> BreachRegimeLabel:
    return _BREACH_LABEL_MAP[label]


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
    # Lazy-import via ``alphamind.execution.oms`` so the package's
    # ``__getattr__`` runs ``importlib.import_module`` — avoids the circular
    # path that ``from ... .submit_engine_envelope import ...`` would trigger
    # when the caller loads us mid-portfolio_manager package init.
    import importlib

    engine_module = importlib.import_module("alphamind.execution.oms.submit_engine_envelope")
    submit_engine_envelope = engine_module.submit_engine_envelope
    build_initial_submit_engine_envelope_state = (
        engine_module.build_initial_submit_engine_envelope_state
    )

    state = build_initial_submit_engine_envelope_state(monitor_session_id=monitor_session_id)
    id_provider = invocation_id_provider or make_invocation_id_provider_sync(session_factory)

    async def _submit(envelope: OmsEngineEnvelope) -> Any:
        invocation_id = await id_provider()
        async with session_factory() as session:
            handle = InvocationHandle(session=session, invocation_id=invocation_id)
            result = await submit_engine_envelope(
                envelope,
                handle=handle,
                state=state,
                config=state_persistence_config,
            )
            await session.commit()
            return result

    return _submit
