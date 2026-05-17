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

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
)
from alphamind.config.models.regimes import Regime
from alphamind.execution.continuous_monitor.breach_loop.production_substrate import (
    DispatchPlaceholders,
    load_breach_loop_resolved_config,
    make_dispatch_context_provider,
    make_invocation_id_provider_sync,
    make_library_config_factory,
    make_open_positions_view_provider,
    make_regime_provider,
    make_snapshot_provider,
    make_submit_envelope,
)
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_async_engine, make_async_session_factory
from alphamind.portfolio_state import load_portfolio_state_config
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationOutput,
    build_synthetic_regime_output,
)
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
    """The regime provider wraps the latest invocation's parameter set."""

    async def test_empty_db_falls_back_to_bootstrap_regime(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        config_dir: Path,
    ) -> None:
        """No invocations in the DB → bootstrap-regime synthetic output."""
        provider = make_regime_provider(
            session_factory=db_session_factory,
            resolved=load_breach_loop_resolved_config(config_dir),
        )
        output = await provider()
        # The synthetic output is shaped like the orchestrator's pre-review shim.
        assert output.active_risk_parameter_set is not None
        assert output.runtime_dimensions_active_regime.value == "normal"

    async def test_reads_active_regime_from_latest_invocation(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """The provider's output mirrors the latest invocation's persisted regime."""
        snap = tmp_path / "snap.json"
        snap.write_text('{"rule_values": {"daily_drawdown_pct": 2.5}, "regime_label": "elevated"}')
        await _seed_invocation(
            db_session_factory,
            invocation_id="inv-elevated",
            start_at=datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
            snapshot_path=str(snap),
            active_regime="elevated",
        )

        provider = make_regime_provider(
            session_factory=db_session_factory,
            resolved=load_breach_loop_resolved_config(config_dir),
        )
        output = await provider()
        assert output.runtime_dimensions_active_regime.value == "elevated"
        # The parameter set's entries reflect the snapshot file's rule_values.
        entries_by_id = {e.rule_id: e.value for e in output.active_risk_parameter_set.entries}
        assert entries_by_id["daily_drawdown_pct"] == pytest.approx(2.5)


class TestSnapshotProvider:
    """Structural smoke tests — full snapshot assembly is covered by upstream."""

    def test_provider_constructs(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """``make_snapshot_provider`` constructs a callable closure without raising."""
        provider = make_snapshot_provider(
            session_factory=db_session_factory,
            underlying_cache=UnderlyingPriceCache(),
            resolved=load_breach_loop_resolved_config(config_dir),
            portfolio_state_config=load_portfolio_state_config(config_dir / "portfolio_state.yaml"),
            state_persistence_config=StatePersistenceConfig(
                pm_decision_log_sliding_window_invocations=10,
                snapshot_read_timeout_seconds=5.0,
                pip_freeze_snapshot_root=str(tmp_path / "pip"),
                invocation_provenance_root=str(tmp_path / "prov"),
            ),
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
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=10.0 * sign,
        position_age_hours=2.0,
        notional_exposure_usd=money(market_value_usd),
        delta_adjusted_exposure_usd=signed_money(market_value_usd * sign),
        distance_to_target_usd=signed_money(10.0),
        distance_to_stop_usd=signed_money(5.0),
        risk_reward_at_current=2.0,
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
    """Synthetic ``RegimeAdaptationOutput`` for context-provider tests."""
    return build_synthetic_regime_output(
        active_risk_parameters=_active_parameter_set(),
        runtime_active_regime=Regime.normal,
        invocation_id="inv-test",
        now=datetime(2026, 5, 17, 14, 30, tzinfo=UTC),
    )


class TestMakeDispatchContextProvider:
    """The dispatcher's per-tick context-builder threads live deps into BreachDispatchContext."""

    async def test_iv_provider_threaded_into_market_inputs(self, config_dir: Path) -> None:
        """The IV provider the caller passes is the one consumers see on the context."""
        snapshot = _make_library_snapshot()
        regime_output = _make_regime_output()

        async def _snapshot_provider() -> LibrarySnapshot:
            return snapshot

        async def _regime_provider() -> RegimeAdaptationOutput:
            return regime_output

        iv = FixtureIvProvider(surface={}, realized_vol={})
        provider = make_dispatch_context_provider(
            snapshot_provider=_snapshot_provider,
            regime_provider=_regime_provider,
            library_config_factory=make_library_config_factory(
                resolved=load_breach_loop_resolved_config(config_dir),
            ),
            underlying_cache=UnderlyingPriceCache(),
            iv_provider=iv,
            placeholders=DispatchPlaceholders(
                adv_to_position_size_ratio=10.0, risk_reward_ratio=2.0
            ),
        )
        context = await provider()
        # ``BreachDispatchContext.market_inputs`` is typed as the protocol;
        # cast to the concrete ``MarketInputs`` to assert the IV provider
        # identity ran through.
        market_inputs = cast(MarketInputs, context.market_inputs)
        assert market_inputs.iv_provider is iv

    async def test_open_positions_populated_from_provider(self, config_dir: Path) -> None:
        """When ``open_positions_provider`` returns N positions, the context carries N."""
        snapshot = _make_library_snapshot()
        regime_output = _make_regime_output()
        positions = (
            _equity_position_view(position_id="p1", ticker="AAPL"),
            _equity_position_view(position_id="p2", ticker="MSFT"),
        )

        async def _snapshot_provider() -> LibrarySnapshot:
            return snapshot

        async def _regime_provider() -> RegimeAdaptationOutput:
            return regime_output

        async def _open_positions_provider() -> tuple[PositionView, ...]:
            return positions

        provider = make_dispatch_context_provider(
            snapshot_provider=_snapshot_provider,
            regime_provider=_regime_provider,
            library_config_factory=make_library_config_factory(
                resolved=load_breach_loop_resolved_config(config_dir),
            ),
            underlying_cache=UnderlyingPriceCache(),
            iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
            placeholders=DispatchPlaceholders(
                adv_to_position_size_ratio=10.0, risk_reward_ratio=2.0
            ),
            open_positions_provider=_open_positions_provider,
        )
        context = await provider()
        assert context.open_positions == positions

    async def test_open_positions_empty_when_provider_absent(self, config_dir: Path) -> None:
        """No ``open_positions_provider`` → empty ``open_positions`` and zero liquidity/R/R."""
        snapshot = _make_library_snapshot()
        regime_output = _make_regime_output()

        async def _snapshot_provider() -> LibrarySnapshot:
            return snapshot

        async def _regime_provider() -> RegimeAdaptationOutput:
            return regime_output

        provider = make_dispatch_context_provider(
            snapshot_provider=_snapshot_provider,
            regime_provider=_regime_provider,
            library_config_factory=make_library_config_factory(
                resolved=load_breach_loop_resolved_config(config_dir),
            ),
            underlying_cache=UnderlyingPriceCache(),
            iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
            placeholders=DispatchPlaceholders(
                adv_to_position_size_ratio=10.0, risk_reward_ratio=2.0
            ),
        )
        context = await provider()
        assert context.open_positions == ()
        assert context.liquidity == ()
        assert context.risk_reward_metric == ()

    async def test_placeholder_ratios_apply_per_position(self, config_dir: Path) -> None:
        """Each PositionLiquidity / PositionRiskReward carries the configured placeholder."""
        snapshot = _make_library_snapshot()
        regime_output = _make_regime_output()
        positions = (
            _equity_position_view(position_id="p1", ticker="AAPL"),
            _equity_position_view(position_id="p2", ticker="MSFT"),
        )

        async def _snapshot_provider() -> LibrarySnapshot:
            return snapshot

        async def _regime_provider() -> RegimeAdaptationOutput:
            return regime_output

        async def _open_positions_provider() -> tuple[PositionView, ...]:
            return positions

        provider = make_dispatch_context_provider(
            snapshot_provider=_snapshot_provider,
            regime_provider=_regime_provider,
            library_config_factory=make_library_config_factory(
                resolved=load_breach_loop_resolved_config(config_dir),
            ),
            underlying_cache=UnderlyingPriceCache(),
            iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
            placeholders=DispatchPlaceholders(
                adv_to_position_size_ratio=42.0, risk_reward_ratio=7.5
            ),
            open_positions_provider=_open_positions_provider,
        )
        context = await provider()
        assert tuple(liq.adv_to_position_size_ratio for liq in context.liquidity) == (42.0, 42.0)
        assert tuple(r.risk_reward_ratio for r in context.risk_reward_metric) == (7.5, 7.5)


class TestMakeOpenPositionsViewProvider:
    """The PositionView-tuple provider feeds the dispatcher's context per immediate breach."""

    async def test_empty_db_returns_empty(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """No invocation row → empty tuple (bootstrap path, no cascade fires)."""
        provider = make_open_positions_view_provider(
            session_factory=db_session_factory,
            underlying_cache=UnderlyingPriceCache(),
            resolved=load_breach_loop_resolved_config(config_dir),
            portfolio_state_config=load_portfolio_state_config(config_dir / "portfolio_state.yaml"),
            state_persistence_config=StatePersistenceConfig(
                pm_decision_log_sliding_window_invocations=10,
                snapshot_read_timeout_seconds=5.0,
                pip_freeze_snapshot_root=str(tmp_path / "pip"),
                invocation_provenance_root=str(tmp_path / "prov"),
            ),
        )
        assert await provider() == ()

    def test_provider_constructs(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        config_dir: Path,
        tmp_path: Path,
    ) -> None:
        """``make_open_positions_view_provider`` returns a callable closure."""
        provider = make_open_positions_view_provider(
            session_factory=db_session_factory,
            underlying_cache=UnderlyingPriceCache(),
            resolved=load_breach_loop_resolved_config(config_dir),
            portfolio_state_config=load_portfolio_state_config(config_dir / "portfolio_state.yaml"),
            state_persistence_config=StatePersistenceConfig(
                pm_decision_log_sliding_window_invocations=10,
                snapshot_read_timeout_seconds=5.0,
                pip_freeze_snapshot_root=str(tmp_path / "pip"),
                invocation_provenance_root=str(tmp_path / "prov"),
            ),
        )
        assert callable(provider)
