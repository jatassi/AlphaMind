"""Tests for the breach-loop production substrate (ALP-453).

The substrate module replaces ``_register_breach_loop``'s ``_empty_*``
provider stubs with real factories backed by:

* :class:`SqlPortfolioStateRepository` + :func:`assemble_snapshot` for the
  per-tick library snapshot.
* The latest-invocation pattern (mirroring greeks_refresh) for the
  invocation id and active-risk-parameters.
* :func:`from_resolved_config` for the base library-config seed, refreshed
  per-tick from the active risk parameter set's effective limits.
* :func:`submit_engine_envelope` for the dispatch path, threaded with an
  :class:`InvocationHandle` factory the monitor process owns.

These tests pin the helpers' wiring contracts; the underlying primitives'
correctness is the existing test surface of each subsystem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

# Import portfolio_manager.models first to break the latent cycle between
# alphamind.execution.oms (engine-stub MCP) and alphamind.decision.portfolio_manager.
# Mirrors the breaker comment in tests/execution/oms/test_submit_engine_envelope.py.
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind._kernel.ids import (
    BracketId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.config.models.regimes import Regime
from alphamind.execution.continuous_monitor.breach_loop.production_substrate import (
    DispatchContextProvider,
    _build_price_provider,
    load_breach_loop_resolved_config,
    make_adv_provider,
    make_assembled_snapshot_provider,
    make_dispatch_context_provider,
    make_invocation_id_provider_sync,
    make_library_config_factory,
    make_library_snapshot_translator,
    make_regime_provider,
    make_snapshot_provider,
    make_submit_envelope,
)
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.persistence.models import AssetUniverse, Base, OhlcvBars
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
)
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.freshness import AssembledSnapshot, SnapshotFreshness
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    select_for_drawdown_breach,
    select_for_margin_call,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationConfigFan,
    RegimeAdaptationOutput,
    load_config_fan,
)
from alphamind.risk_guardrails.regime_adaptation.persistence import insert_state
from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationState
from alphamind.scripts._common import load_distillation_config
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow


@pytest.fixture()
async def db_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "alphamind.db"
    engine = make_async_engine(str(db_path))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_async_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def _build_regime_config_fan(config_dir: Path) -> RegimeAdaptationConfigFan:
    """Construct the resolver's slow-changing config fan for the test."""
    from alphamind.execution.continuous_monitor.breach_loop.production_substrate import (
        load_breach_loop_loaded_config,
    )

    return load_config_fan(
        config_dir=config_dir,
        loaded_config=load_breach_loop_loaded_config(config_dir),
        distillation_config=load_distillation_config(config_dir / "distillation.yaml"),
    )


@pytest.fixture()
def config_dir() -> Path:
    """Shipped config dir — every YAML loader runs against the repo's authoritative state."""
    return Path(__file__).resolve().parents[4] / "config"


def _active_parameter_set(*, daily_drawdown_pct: float = 1.5) -> ActiveRiskParameterSet:
    """An ``ActiveRiskParameterSet`` whose entries the factory copies into effective_limits."""
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="daily_drawdown_pct",
                value=daily_drawdown_pct,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=daily_drawdown_pct,
            ),
        ),
        active_overlays=(),
    )


# ---------------------------------------------------------------------------
# Minimal :class:`AssembledSnapshot` builder for substrate-wiring tests.
# The dispatch-context-provider tests stub ``library_snapshot_translator`` so
# only ``snapshot.open_positions`` is read off the assembled snapshot — the
# remaining fields just need to satisfy :class:`PortfolioStateSnapshot`'s
# ``__post_init__`` validators.
# ---------------------------------------------------------------------------

_SNAPSHOT_AT = datetime(2026, 5, 17, 14, 30, tzinfo=UTC)


def _make_assembled_snapshot(
    *,
    positions: tuple[PositionView, ...] = (),
) -> AssembledSnapshot:
    """Build a minimal valid :class:`AssembledSnapshot` for substrate tests."""
    snapshot = PortfolioStateSnapshot(
        invocation_id="inv-test",
        fill_collection_committed_at=_SNAPSHOT_AT,
        snapshot_assembled_at=_SNAPSHOT_AT,
        open_positions=positions,
        pending_positions=(),
        sector_exposure=(),
        directional_exposure=DirectionalExposure(
            total_long_delta_adjusted_usd=signed_money(0.0),
            total_short_delta_adjusted_usd=signed_money(0.0),
            net_directional_pct_of_portfolio=0.0,
            gross_pct_of_portfolio=0.0,
        ),
        portfolio_pnl=PortfolioPnL(
            total_unrealized_pnl_usd=signed_money(0.0),
            total_unrealized_pnl_pct_of_portfolio=0.0,
            daily_realized_pnl_usd=signed_money(0.0),
            daily_total_pnl_usd=signed_money(0.0),
            cumulative_realized_pnl_usd=signed_money(0.0),
            rolling_realized_pnl={
                "1d": signed_money(0.0),
                "3d": signed_money(0.0),
                "5d": signed_money(0.0),
                "20d": signed_money(0.0),
            },
            win_rate_pct=None,
            average_win_size_usd=None,
            average_loss_size_usd=None,
            profit_factor=None,
        ),
        drawdown=DrawdownState(
            current_drawdown_pct=0.0,
            equity_high_water_mark_usd=100_000.0,
            drawdown_duration_hours=0.0,
            lifetime_max_drawdown_pct=0.0,
            intraday_drawdown_pct=0.0,
            daily_zone=RiskZone.NORMAL,
            cumulative_zone=RiskZone.NORMAL,
            cumulative_tier=None,
            drawdown_by_source_pct={},
        ),
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=CashLedger(
            current_cash_usd=100_000.0,
            settled_cash_usd=100_000.0,
            reserved_capital_usd=0.0,
            available_buying_power_usd=100_000.0,
            margin_held_usd=0.0,
            unsettled_proceeds=(),
            cash_pct_of_portfolio=100.0,
            true_deployable_capital_usd=100_000.0,
            regt_excess_trailing_30d_usd=0.0,
            regt_excess_trailing_90d_usd=0.0,
            regt_excess_lifetime_usd=0.0,
        ),
        pending_orders=(),
        risk_budget=RiskBudgetConsumption(entries=()),
        active_risk_parameters=_active_parameter_set(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=ThesisQualityAggregate(
            as_of_timestamp=_SNAPSHOT_AT,
            resolution_counts_by_window=(),
            duration_stats_by_window=(),
            invalidation_timing_stats_by_window=(),
            signal_hit_rates=(),
            signal_to_thesis_conversions=(),
            conviction_calibration=(),
            conviction_sizing_deviation_by_window=(),
            performance_attribution=(),
            alpha_beta_decomposition_by_window=(),
        ),
        brackets=(),
    )
    freshness = SnapshotFreshness(
        fill_collection_committed_at=_SNAPSHOT_AT,
        snapshot_assembled_at=_SNAPSHOT_AT,
        fill_collection_to_snapshot_seconds=0.0,
        max_fill_collection_to_snapshot_seconds=30.0,
        fill_collection_to_snapshot_within_threshold=True,
        total_open_positions=len(positions),
        total_pending_positions=0,
        total_positions=len(positions),
        position_ids_priced_fresh=frozenset(p.position_id for p in positions),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        count_priced_fresh=len(positions),
        count_priced_stale=0,
        count_unknown_ticker=0,
        all_position_prices_fresh=True,
        oldest_price_as_of=None,
        oldest_price_age_seconds=None,
        max_price_age_seconds=900.0,
    )
    return AssembledSnapshot(snapshot=snapshot, freshness=freshness, price_map={})


class TestMakeLibraryConfigFactory:
    """The factory adapts an :class:`ActiveRiskParameterSet` → :class:`LibraryConfig`."""

    def test_effective_limits_track_active_parameter_set(self, config_dir: Path) -> None:
        """Per-call, ``effective_limits`` reflect the supplied set's entries."""
        factory = make_library_config_factory(resolved=load_breach_loop_resolved_config(config_dir))
        cfg = factory(_active_parameter_set(daily_drawdown_pct=2.5))
        assert cfg.effective_limits["daily_drawdown_pct"] == pytest.approx(2.5)

    def test_two_calls_with_different_sets_yield_different_limits(self, config_dir: Path) -> None:
        """The factory is pure; two calls with distinct sets produce distinct configs."""
        factory = make_library_config_factory(resolved=load_breach_loop_resolved_config(config_dir))
        a = factory(_active_parameter_set(daily_drawdown_pct=1.0))
        b = factory(_active_parameter_set(daily_drawdown_pct=3.0))
        assert a.effective_limits["daily_drawdown_pct"] == pytest.approx(1.0)
        assert b.effective_limits["daily_drawdown_pct"] == pytest.approx(3.0)

    def test_escalation_zones_seeded_from_guardrails_yaml(self, config_dir: Path) -> None:
        """``escalation_zones`` come from ``config/guardrails.yaml`` (not empty)."""
        factory = make_library_config_factory(resolved=load_breach_loop_resolved_config(config_dir))
        cfg = factory(_active_parameter_set())
        # Every rule that appears in effective_limits has an escalation_zones entry.
        assert set(cfg.effective_limits.keys()).issubset(set(cfg.escalation_zones.keys()))

    def test_feature_flags_carved_from_resolved_config(self, config_dir: Path) -> None:
        """Feature flags propagate from the shipped configs."""
        factory = make_library_config_factory(resolved=load_breach_loop_resolved_config(config_dir))
        cfg = factory(_active_parameter_set())
        # The flag is sourced from the resolved config; carving must produce a real view.
        assert isinstance(cfg.feature_flags.options_enabled, bool)
        assert isinstance(cfg.feature_flags.short_selling_enabled, bool)


# ---------------------------------------------------------------------------
# DB seed helpers — used by the invocation-id, regime, and snapshot tests
# ---------------------------------------------------------------------------


def _process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="monitor",
        process_start_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC).isoformat(),
        process_pid=12345,
        hostname="host",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/pip.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Darwin-25.4.0",
    )


def _invocation_row(
    *,
    invocation_id: str,
    start_at: datetime,
    resolved_config_snapshot_path: str,
    active_regime: str = "normal",
) -> InvocationRow:
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at=start_at.isoformat(),
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime=active_regime,
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=resolved_config_snapshot_path,
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/calibration.json",
        data_source_freshness_json="{}",
    )


async def _seed_invocation(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
    start_at: datetime,
    snapshot_path: str,
    active_regime: str = "normal",
) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime_record()))
        await sess.commit()
    async with factory() as sess:
        sess.add(
            _invocation_row(
                invocation_id=invocation_id,
                start_at=start_at,
                resolved_config_snapshot_path=snapshot_path,
                active_regime=active_regime,
            )
        )
        await sess.commit()


class TestInvocationIdProvider:
    """The sync provider mirrors the greeks_refresh / emergency_trigger pattern."""

    async def test_empty_db_returns_bootstrap_sentinel(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        provider = make_invocation_id_provider_sync(db_session_factory)
        assert await provider() == "monitor-bootstrap"

    async def test_returns_latest_invocation_id(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        snap = tmp_path / "snap.json"
        snap.write_text('{"rule_values": {"daily_drawdown_pct": 1.0}, "regime_label": "normal"}')
        await _seed_invocation(
            db_session_factory,
            invocation_id="inv-old",
            start_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
            snapshot_path=str(snap),
        )
        async with db_session_factory() as sess:
            sess.add(
                _invocation_row(
                    invocation_id="inv-latest",
                    start_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
                    resolved_config_snapshot_path=str(snap),
                )
            )
            await sess.commit()

        provider = make_invocation_id_provider_sync(db_session_factory)
        assert await provider() == "inv-latest"


class TestRegimeProvider:
    """The regime provider calls :func:`resolve_regime_adaptation` per tick."""

    async def test_empty_db_falls_back_to_bootstrap_regime(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        config_dir: Path,
    ) -> None:
        """No invocations in the DB → bootstrap-regime synthetic output."""
        resolved = load_breach_loop_resolved_config(config_dir)
        provider = make_regime_provider(
            session_factory=db_session_factory,
            sync_session_factory=sync_session_factory,
            config_fan=_build_regime_config_fan(config_dir),
            resolved=resolved,
        )
        output = await provider()
        assert output.active_risk_parameter_set is not None
        assert output.runtime_dimensions_active_regime.value == "normal"

    async def test_persisted_state_drives_resolver_output(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        sync_session_factory: sessionmaker[Session],
        config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """With a persisted regime state row, the resolver maps it through to output."""
        snap = tmp_path / "snap.json"
        snap.write_text('{"rule_values": {"daily_drawdown_pct": 2.5}, "regime_label": "elevated"}')
        await _seed_invocation(
            db_session_factory,
            invocation_id="inv-elevated",
            start_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
            snapshot_path=str(snap),
            active_regime="elevated",
        )
        # Seed the regime-adaptation state the resolver reads back as ``prior_state``.
        with sync_session_factory() as session:
            insert_state(
                session,
                RegimeAdaptationState(
                    as_of="2026-05-11T09:00:00Z",
                    invocation_id="inv-elevated",
                    active_regime=Regime.elevated,
                    prior_regime=None,
                    transition_state=RegimeTransitionState.STABLE,
                    transition_invocations_remaining=0,
                    transition_started_invocation_id=None,
                    transition_origin_regime=None,
                    active_overlays=(),
                    distillation_regime_label="vol_expansion",
                    distillation_vix_level=25.0,
                    regime_skip_emergency=False,
                ),
                ingested_at="2026-05-11T09:00:01Z",
            )

        provider = make_regime_provider(
            session_factory=db_session_factory,
            sync_session_factory=sync_session_factory,
            config_fan=_build_regime_config_fan(config_dir),
            resolved=load_breach_loop_resolved_config(config_dir),
        )
        output = await provider()
        assert output.runtime_dimensions_active_regime is Regime.elevated


class TestSnapshotProvider:
    """Structural smoke tests — full snapshot assembly is covered by upstream."""

    def test_provider_constructs(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """``make_snapshot_provider`` constructs a callable closure without raising."""
        resolved = load_breach_loop_resolved_config(config_dir)
        assembled = make_assembled_snapshot_provider(
            session_factory=db_session_factory,
            underlying_cache=UnderlyingPriceCache(),
            resolved=resolved,
            portfolio_state_config=load_portfolio_state_config(config_dir / "portfolio_state.yaml"),
            state_persistence_config=StatePersistenceConfig(
                pm_decision_log_sliding_window_invocations=10,
                snapshot_read_timeout_seconds=5.0,
                pip_freeze_snapshot_root=str(tmp_path / "pip"),
                invocation_provenance_root=str(tmp_path / "prov"),
            ),
        )
        provider = make_snapshot_provider(
            assembled_snapshot_provider=assembled,
            library_snapshot_translator=make_library_snapshot_translator(resolved=resolved),
        )
        assert callable(provider)


class TestSubmitEnvelope:
    """Structural smoke test — full submit path covered by ``test_submit_engine_envelope``."""

    def test_submit_envelope_constructs(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """``make_submit_envelope`` returns a callable bound to the supplied session id."""
        submit = make_submit_envelope(
            session_factory=db_session_factory,
            monitor_session_id="mon-test",
            state_persistence_config=StatePersistenceConfig(
                pm_decision_log_sliding_window_invocations=10,
                snapshot_read_timeout_seconds=5.0,
                pip_freeze_snapshot_root=str(tmp_path / "pip"),
                invocation_provenance_root=str(tmp_path / "prov"),
            ),
        )
        assert callable(submit)

    async def test_concurrent_submits_serialize_state_rebind(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """F11: Two concurrent calls into the submit closure must not race on
        the nonlocal ``state`` rebind. The asyncio.Lock serializes them so
        each call observes the prior caller's state when computing dedup.
        """
        import asyncio as _asyncio

        from alphamind.execution.oms import submit_engine_envelope as _sub_mod

        # Patch the closure-captured ``submit_engine_envelope`` symbol the
        # closure imports inline at call time. The substitute yields back to
        # the event loop mid-call so a second concurrent _submit() call would
        # otherwise re-read the stale ``state`` cell.
        seen_states: list[Any] = []

        async def fake_submit_engine_envelope(
            envelope: Any,
            *,
            handle: Any,
            state: Any,
            config: Any,
        ) -> tuple[str, Any]:
            del handle, config
            # Record the state the caller observed; the lock must ensure each
            # caller sees the rebound state from the prior caller.
            seen_states.append(state)
            # Yield to let any concurrently-pending call attempt to enter.
            await _asyncio.sleep(0)
            # Return a new state object so the rebind is observable.
            new_state = type(state)(
                monitor_session_id=state.monitor_session_id,
                seen_trigger_ids=state.seen_trigger_ids | {len(seen_states)},
            )
            return ("ok", new_state)

        monkeypatch.setattr(
            _sub_mod,
            "submit_engine_envelope",
            fake_submit_engine_envelope,
        )

        async def fake_id_provider() -> str:
            return "inv-test"

        submit = make_submit_envelope(
            session_factory=db_session_factory,
            monitor_session_id="mon-test",
            state_persistence_config=StatePersistenceConfig(
                pm_decision_log_sliding_window_invocations=10,
                snapshot_read_timeout_seconds=5.0,
                pip_freeze_snapshot_root=str(tmp_path / "pip"),
                invocation_provenance_root=str(tmp_path / "prov"),
            ),
            invocation_id_provider=fake_id_provider,
        )

        # Two concurrent calls; the second must NOT enter with the same state
        # the first saw (otherwise dedup would be broken).
        await _asyncio.gather(submit(cast(Any, object())), submit(cast(Any, object())))

        # The second observed state's seen_trigger_ids must include the
        # element added by the first call's rebind — proves serialization.
        assert len(seen_states) == 2
        assert seen_states[1].seen_trigger_ids == frozenset({1})
        assert seen_states[0].seen_trigger_ids == frozenset()


# ---------------------------------------------------------------------------
# make_dispatch_context_provider
# ---------------------------------------------------------------------------


def _equity_position_view(
    *,
    position_id: str,
    ticker: str = "NVDA",
    direction: Direction = Direction.LONG,
    share_count: float = 10.0,
    cost_basis: float = 150.0,
    market_value_usd: float = 1500.0,
    unrealized_pnl_usd: float = 0.0,
    risk_reward_at_current: float | None = 2.0,
) -> PositionView:
    """Minimal open equity ``PositionView`` for dispatch-context tests."""
    as_of = datetime(2026, 5, 17, 14, 30, tzinfo=UTC)
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=cost_basis,
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THE-{position_id}"),
        bracket_id=BracketId(f"BRK-{position_id}"),
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=as_of,
        details=details,
        execution_history=(
            PositionFill(
                fill_timestamp=as_of,
                fill_price=price(cost_basis),
                fill_quantity=share_count,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    sign = 1.0 if direction == Direction.LONG else -1.0
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(market_value_usd),
        unrealized_pnl_usd=signed_money(unrealized_pnl_usd),
        unrealized_pnl_pct=0.0,
        position_weight_pct=10.0 * sign,
        position_age_hours=2.0,
        notional_exposure_usd=money(market_value_usd),
        delta_adjusted_exposure_usd=signed_money(market_value_usd * sign),
        distance_to_target_usd=signed_money(10.0),
        distance_to_stop_usd=signed_money(5.0),
        risk_reward_at_current=risk_reward_at_current,
    )


def _make_library_snapshot(*, portfolio_value_usd: float = 100_000.0) -> LibrarySnapshot:
    """Construct a minimal :class:`LibrarySnapshot` for context-provider tests."""
    return LibrarySnapshot(
        portfolio_value_usd=portfolio_value_usd,
        cash_usd=10_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct={},
        net_long_pct=10.0,
        net_short_pct=0.0,
        gross_pct=10.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=10.0,
        existing_positions={},
    )


def _make_regime_output() -> RegimeAdaptationOutput:
    """Minimal ``RegimeAdaptationOutput`` for context-provider tests.

    Context-provider tests only read ``active_risk_parameter_set`` off the
    output; the remaining fields just need to satisfy the dataclass
    contract. Constructed inline to avoid a dependency on either the
    retired synthetic shim or a full ``resolve_regime_adaptation`` run.
    """
    now = datetime(2026, 5, 17, 14, 30, tzinfo=UTC)
    state = RegimeAdaptationState(
        as_of=now.isoformat().replace("+00:00", "Z"),
        invocation_id="inv-test",
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label=Regime.normal.value,
        distillation_vix_level=0.0,
        regime_skip_emergency=False,
    )
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=_active_parameter_set(),
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=state,
        audit_log_entries=(),
    )


async def _empty_adv_provider() -> Mapping[str, float]:
    """ADV provider returning no entries — every position falls back to zero ratio."""
    return {}


async def _build_dispatch_context_provider(
    *,
    config_dir: Path,
    positions: tuple[PositionView, ...] | None = None,
    adv_map: Mapping[str, float] | None = None,
    quotes: Mapping[str, float] | None = None,
    iv_provider: FixtureIvProvider | None = None,
    assembled_snapshot_call_count: list[int] | None = None,
) -> DispatchContextProvider:
    """Construct ``make_dispatch_context_provider`` with stubs around the varying inputs.

    ``positions=None`` (default) yields an :class:`AssembledSnapshot` with
    empty ``open_positions``; an explicit empty tuple is equivalent. The
    ``library_snapshot_translator`` stub returns a pre-built
    :class:`LibrarySnapshot`, so the assembled snapshot's other fields only
    need to satisfy :class:`PortfolioStateSnapshot`'s validators.

    Pass ``assembled_snapshot_call_count`` (single-element ``[0]``) to
    count how many times the assembled-snapshot provider is awaited per
    dispatch (regression test for ALP-510).
    """
    as_of = datetime(2026, 5, 17, 14, 30, tzinfo=UTC)
    cache = UnderlyingPriceCache()
    for ticker, price_usd in (quotes or {}).items():
        await cache.update(UnderlyingQuote(ticker=ticker, price=price_usd, as_of=as_of))

    assembled = _make_assembled_snapshot(positions=positions or ())
    library_snapshot = _make_library_snapshot()
    regime_output = _make_regime_output()

    async def _assembled_provider() -> AssembledSnapshot:
        if assembled_snapshot_call_count is not None:
            assembled_snapshot_call_count[0] += 1
        return assembled

    def _translator(_snapshot: PortfolioStateSnapshot) -> LibrarySnapshot:
        return library_snapshot

    async def _regime_provider() -> RegimeAdaptationOutput:
        return regime_output

    async def _adv_provider() -> Mapping[str, float]:
        return dict(adv_map) if adv_map else {}

    return make_dispatch_context_provider(
        assembled_snapshot_provider=_assembled_provider,
        library_snapshot_translator=_translator,
        regime_provider=_regime_provider,
        library_config_factory=make_library_config_factory(
            resolved=load_breach_loop_resolved_config(config_dir),
        ),
        underlying_cache=cache,
        iv_provider=iv_provider or FixtureIvProvider(surface={}, realized_vol={}),
        adv_provider=_adv_provider,
    )


class TestMakeDispatchContextProvider:
    """The dispatcher's per-tick context-builder threads live deps into BreachDispatchContext."""

    async def test_iv_provider_threaded_into_market_inputs(self, config_dir: Path) -> None:
        """The IV provider the caller passes is the one consumers see on the context."""
        iv = FixtureIvProvider(surface={}, realized_vol={})
        provider = await _build_dispatch_context_provider(config_dir=config_dir, iv_provider=iv)
        context = await provider()
        # ``BreachDispatchContext.market_inputs`` is typed as the protocol;
        # cast to the concrete ``MarketInputs`` to assert the IV provider
        # identity ran through.
        market_inputs = cast(MarketInputs, context.market_inputs)
        assert market_inputs.iv_provider is iv

    async def test_open_positions_populated_from_assembled_snapshot(self, config_dir: Path) -> None:
        """N positions on the assembled snapshot → N positions on the context."""
        positions = (
            _equity_position_view(position_id="p1", ticker="AAPL"),
            _equity_position_view(position_id="p2", ticker="MSFT"),
        )
        provider = await _build_dispatch_context_provider(
            config_dir=config_dir, positions=positions
        )
        context = await provider()
        assert context.open_positions == positions

    async def test_open_positions_empty_when_assembled_snapshot_has_none(
        self, config_dir: Path
    ) -> None:
        """Assembled snapshot with no open positions → empty open_positions / liquidity / R/R."""
        provider = await _build_dispatch_context_provider(config_dir=config_dir)
        context = await provider()
        assert context.open_positions == ()
        assert context.liquidity == ()
        assert context.risk_reward_metric == ()

    async def test_liquidity_and_rr_computed_per_position_from_live_signals(
        self, config_dir: Path
    ) -> None:
        """Liquidity = ADV*price/notional from the live map; R/R from PositionView.

        Each position carries a distinct ticker, notional, and
        ``risk_reward_at_current`` value so a uniform-placeholder regression
        would surface as both ratios collapsing to the same constant.
        """
        positions = (
            _equity_position_view(
                position_id="p1",
                ticker="AAPL",
                share_count=10.0,
                market_value_usd=1500.0,
                risk_reward_at_current=2.5,
            ),
            _equity_position_view(
                position_id="p2",
                ticker="MSFT",
                share_count=5.0,
                market_value_usd=750.0,
                risk_reward_at_current=1.0,
            ),
        )
        provider = await _build_dispatch_context_provider(
            config_dir=config_dir,
            positions=positions,
            adv_map={"AAPL": 1_000_000.0, "MSFT": 2_000_000.0},
            quotes={"AAPL": 150.0, "MSFT": 300.0},
        )
        context = await provider()
        assert tuple(liq.adv_to_position_size_ratio for liq in context.liquidity) == (
            100_000.0,
            800_000.0,
        )
        assert tuple(r.risk_reward_ratio for r in context.risk_reward_metric) == (2.5, 1.0)

    async def test_drawdown_selector_picks_higher_liquidity_position_via_live_adv(
        self, config_dir: Path
    ) -> None:
        """When two losers tie on P/L, the substrate-derived ADV picks the more liquid one.

        Exercises the per-position-ADV selector branch end-to-end: each
        position carries the same loss, so the selector falls through to its
        liquidity tiebreaker and selects the position whose ticker has higher
        ADV in the live map.
        """
        positions = (
            _equity_position_view(
                position_id="p-msft",
                ticker="MSFT",
                share_count=10.0,
                market_value_usd=1000.0,
                unrealized_pnl_usd=-100.0,
            ),
            _equity_position_view(
                position_id="p-aapl",
                ticker="AAPL",
                share_count=10.0,
                market_value_usd=1000.0,
                unrealized_pnl_usd=-100.0,
            ),
        )
        provider = await _build_dispatch_context_provider(
            config_dir=config_dir,
            positions=positions,
            adv_map={"AAPL": 10_000_000.0, "MSFT": 1_000_000.0},
            quotes={"AAPL": 150.0, "MSFT": 300.0},
        )
        context = await provider()

        result = select_for_drawdown_breach(
            open_positions=context.open_positions,
            liquidity=context.liquidity,
        )
        assert result.position_id == "p-aapl"

    async def test_margin_call_selector_picks_worst_rr_via_live_position_view(
        self, config_dir: Path
    ) -> None:
        """The margin-call selector reads per-position R/R sourced from PositionView.

        Two positions with different ``risk_reward_at_current`` values flow
        through the substrate; the selector must pick the lower-R/R one
        (closest to invalidation, farthest from target).
        """
        positions = (
            _equity_position_view(
                position_id="p-safe",
                ticker="AAPL",
                risk_reward_at_current=3.0,
            ),
            _equity_position_view(
                position_id="p-risky",
                ticker="MSFT",
                risk_reward_at_current=0.5,
            ),
        )
        provider = await _build_dispatch_context_provider(
            config_dir=config_dir,
            positions=positions,
            adv_map={"AAPL": 1_000_000.0, "MSFT": 1_000_000.0},
            quotes={"AAPL": 150.0, "MSFT": 300.0},
        )
        context = await provider()

        result = select_for_margin_call(
            open_positions=context.open_positions,
            liquidity=context.liquidity,
            additional_margin_required_usd=5_000.0,
            risk_reward_metric=context.risk_reward_metric,
        )
        assert result.position_id == "p-risky"

    async def test_missing_adv_and_rr_default_to_zero(self, config_dir: Path) -> None:
        """Position missing from the ADV map and lacking R/R defaults to 0.0 on both."""
        positions = (
            _equity_position_view(
                position_id="p-no-data",
                ticker="UNKNOWN",
                risk_reward_at_current=None,
            ),
        )
        provider = await _build_dispatch_context_provider(
            config_dir=config_dir, positions=positions
        )
        context = await provider()
        assert context.liquidity[0].adv_to_position_size_ratio == 0.0
        assert context.risk_reward_metric[0].risk_reward_ratio == 0.0

    async def test_assembled_snapshot_provider_awaited_once_per_dispatch(
        self, config_dir: Path
    ) -> None:
        """ALP-510 regression — the assemble pipeline runs once per dispatch, not twice.

        Before ALP-510, the dispatch context provider awaited
        ``snapshot_provider()`` and ``open_positions_provider()`` independently,
        each running :func:`assemble_snapshot` end-to-end. After the refactor,
        both library snapshot and open positions are derived from a single
        :class:`AssembledSnapshot`, so the assembled-snapshot provider is
        awaited exactly once.
        """
        positions = (_equity_position_view(position_id="p1", ticker="AAPL"),)
        call_count = [0]
        provider = await _build_dispatch_context_provider(
            config_dir=config_dir,
            positions=positions,
            assembled_snapshot_call_count=call_count,
        )
        await provider()
        assert call_count[0] == 1


async def _seed_universe_ticker(factory: async_sessionmaker[AsyncSession], *, ticker: str) -> None:
    """Insert one ``asset_universe`` row so ``ohlcv_bars`` FK inserts succeed."""
    async with factory() as sess:
        sess.add(
            AssetUniverse(
                asset_id=f"asset-{ticker.lower()}",
                ticker=ticker,
                full_name=f"{ticker} Holdings",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-05-17T00:00:00Z",
            )
        )
        await sess.commit()


async def _seed_ohlcv_bars(
    factory: async_sessionmaker[AsyncSession],
    *,
    ticker: str,
    daily_volumes: tuple[int, ...],
    timeframe: str = "1d",
    start_date: str = "2026-04-01",
) -> None:
    """Insert one daily bar per entry in ``daily_volumes`` (oldest → newest)."""
    from datetime import date, timedelta

    base = date.fromisoformat(start_date)
    async with factory() as sess:
        for offset, volume in enumerate(daily_volumes):
            period_start = (base + timedelta(days=offset)).isoformat()
            sess.add(
                OhlcvBars(
                    ticker=ticker,
                    timeframe=timeframe,
                    period_start=period_start,
                    period_end=period_start,
                    session="regular",
                    adj_open=100.0,
                    adj_high=101.0,
                    adj_low=99.0,
                    adj_close=100.0,
                    adj_volume=volume,
                    unadj_open=100.0,
                    unadj_high=101.0,
                    unadj_low=99.0,
                    unadj_close=100.0,
                    unadj_volume=volume,
                    source="polygon",
                    ingested_at=period_start,
                )
            )
        await sess.commit()


class TestMakeAdvProvider:
    """``make_adv_provider`` returns a per-ticker trailing-N-day ADV map from OhlcvBars."""

    async def test_empty_db_returns_empty_map(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        provider = make_adv_provider(session_factory=db_session_factory)
        assert await provider() == {}

    async def test_average_of_daily_adj_volume_per_ticker(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The map averages each ticker's adj_volume across its daily bars."""
        await _seed_universe_ticker(db_session_factory, ticker="AAPL")
        await _seed_universe_ticker(db_session_factory, ticker="MSFT")
        # AAPL mean over (100, 200, 300) is 200; MSFT mean over (500, 700) is 600.
        await _seed_ohlcv_bars(db_session_factory, ticker="AAPL", daily_volumes=(100, 200, 300))
        await _seed_ohlcv_bars(db_session_factory, ticker="MSFT", daily_volumes=(500, 700))
        provider = make_adv_provider(session_factory=db_session_factory)
        result = await provider()
        assert result["AAPL"] == pytest.approx(200.0)
        assert result["MSFT"] == pytest.approx(600.0)

    async def test_lookback_truncates_to_n_most_recent_bars(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """When more bars exist than ``lookback_days``, only the most recent ones count."""
        await _seed_universe_ticker(db_session_factory, ticker="AAPL")
        # Stale bars then 3 recent bars; 3-day lookback should average only the recent ones.
        await _seed_ohlcv_bars(
            db_session_factory,
            ticker="AAPL",
            daily_volumes=(10, 10, 10, 100, 200, 300),
        )
        provider = make_adv_provider(session_factory=db_session_factory, lookback_days=3)
        result = await provider()
        assert result["AAPL"] == pytest.approx(200.0)

    async def test_non_daily_timeframes_excluded(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Hourly / minute bars do not contribute — only the ``"1d"`` timeframe is averaged."""
        await _seed_universe_ticker(db_session_factory, ticker="AAPL")
        # Hourly bars with very large volume — would distort the mean if included.
        await _seed_ohlcv_bars(
            db_session_factory,
            ticker="AAPL",
            daily_volumes=(1_000_000, 1_000_000),
            timeframe="1h",
        )
        await _seed_ohlcv_bars(db_session_factory, ticker="AAPL", daily_volumes=(100, 200))
        provider = make_adv_provider(session_factory=db_session_factory)
        result = await provider()
        assert result["AAPL"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# _build_price_provider freshness tests (ALP-770)
# ---------------------------------------------------------------------------


class TestBuildPriceProvider:
    """ALP-770 — _build_price_provider must carry the quote's real as_of instead of the
    synthetic current-tick time so the assembler's SnapshotFreshness machinery classifies
    quotes correctly via StubCurrentPriceProvider._recompute."""

    def _cache_with_quote(
        self, *, ticker: str, price: float, quote_time: datetime
    ) -> UnderlyingPriceCache:
        cache = UnderlyingPriceCache()
        # Bypass async update to keep test helpers sync; no concurrent access here.
        cache._quotes[ticker] = UnderlyingQuote(ticker=ticker, price=price, as_of=quote_time)
        return cache

    def test_fresh_quote_carries_real_as_of_not_synthetic(self) -> None:
        """The PriceQuote's as_of_timestamp is the cache quote's timestamp, not the tick time."""
        quote_time = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
        tick_time = datetime(2026, 6, 1, 17, 5, tzinfo=UTC)  # 5 min later
        cache = self._cache_with_quote(ticker="IBM", price=200.0, quote_time=quote_time)

        provider = _build_price_provider(cache, as_of=tick_time)

        # The provider carries the real quote timestamp, not tick_time.
        quote = provider.get_quote("IBM", freshness_threshold_seconds=900.0)
        assert quote.as_of_timestamp == quote_time
        assert quote.as_of_timestamp != tick_time

    def test_fresh_quote_is_not_stale(self) -> None:
        """A quote within the freshness window → is_stale=False via _recompute."""
        quote_time = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
        tick_time = datetime(2026, 6, 1, 17, 5, tzinfo=UTC)  # 5 min (300s) < threshold 900s
        cache = self._cache_with_quote(ticker="IBM", price=200.0, quote_time=quote_time)

        provider = _build_price_provider(cache, as_of=tick_time)

        quote = provider.get_quote("IBM", freshness_threshold_seconds=900.0)
        assert not quote.is_stale

    def test_old_quote_is_stale(self) -> None:
        """A quote older than the threshold → is_stale=True via _recompute (ALP-770 root cause).

        Before ALP-770 the provider stored as_of_timestamp=tick_time (the current tick),
        so _recompute always computed age=0 and is_stale=False. Now it stores the real
        quote.as_of so _recompute sees the actual age.
        """
        quote_time = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
        tick_time = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)  # 3 h = 10800s >> 900s threshold
        cache = self._cache_with_quote(ticker="OXY", price=60.0, quote_time=quote_time)

        provider = _build_price_provider(cache, as_of=tick_time)

        quote = provider.get_quote("OXY", freshness_threshold_seconds=900.0)
        assert quote.is_stale

    def test_absent_ticker_not_in_provider(self) -> None:
        """A ticker absent from the cache is absent from the price provider (no quote)."""
        cache = UnderlyingPriceCache()  # empty
        tick_time = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)

        provider = _build_price_provider(cache, as_of=tick_time)

        from alphamind.portfolio_state.pricing import UnknownTickerError

        with pytest.raises(UnknownTickerError):
            provider.get_quote("IBM", freshness_threshold_seconds=900.0)
