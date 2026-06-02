"""Tests for ``alphamind.scheduler.runtime`` — runtime-dimensions resolver (story 02).

The resolver maps the persistent state of one invocation (prior regime in
``invocations``, current halt state, the two overlay decisions, the firing
trigger) into a :class:`~alphamind.config.resolver.RuntimeDimensions` snapshot
the composition resolver consumes.

Each ``_resolve_*`` helper is exercised independently; the public composer
``resolve_runtime_dimensions`` is covered by an end-to-end test that pins
the four-field output shape.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.modes import Mode
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import RuntimeDimensions
from alphamind.risk_guardrails.breach_behavior.types import HaltState
from alphamind.risk_guardrails.regime_adaptation.types import OverlayActivationDecision
from alphamind.scheduler.runtime import (
    _resolve_active_mode,
    _resolve_active_overlays,
    _resolve_active_regime,
    resolve_runtime_dimensions,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    invocation_record_to_row,
)


def _make_invocation_record(
    *,
    invocation_id: str,
    start_at: str,
    active_regime: str,
    phase2_completed_at: str | None,
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-driver-1",
        start_at=start_at,
        phase1_completed_at=None,
        phase2_completed_at=phase2_completed_at,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime=active_regime,
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/resolved_config.json"
        ),
        feature_flags_snapshot_json='{"foo": true}',
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/data_calibration_state.json"
        ),
        data_source_freshness_json='{"polygon": "2026-05-07T14:00:00Z"}',
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _inactive(overlay: Overlay) -> OverlayActivationDecision:
    return OverlayActivationDecision(
        overlay=overlay,
        is_active=False,
        rationale="",
        pre_event_block_new_positions=False,
    )


def _active(overlay: Overlay) -> OverlayActivationDecision:
    return OverlayActivationDecision(
        overlay=overlay,
        is_active=True,
        rationale=f"{overlay.value} active",
        pre_event_block_new_positions=False,
    )


class TestResolveActiveMode:
    def test_halt_state_none_returns_mode_normal(self) -> None:
        assert _resolve_active_mode(halt_state=None) is Mode.normal

    def test_daily_halt_active_returns_mode_halt(self) -> None:
        halt_state = HaltState(
            daily_halt_active=True,
            cumulative_full_halt_active=False,
            daily_drawdown_pct=0.05,
            daily_drawdown_limit_pct=0.05,
        )
        assert _resolve_active_mode(halt_state=halt_state) is Mode.halt

    def test_cumulative_full_halt_active_returns_mode_halt(self) -> None:
        halt_state = HaltState(
            daily_halt_active=False,
            cumulative_full_halt_active=True,
            daily_drawdown_pct=0.02,
            daily_drawdown_limit_pct=0.05,
        )
        assert _resolve_active_mode(halt_state=halt_state) is Mode.halt


class TestResolveActiveOverlays:
    def test_both_inactive_returns_empty_tuple(self) -> None:
        result = _resolve_active_overlays(
            pre_event_decision=_inactive(Overlay.pre_event),
            stress_decision=_inactive(Overlay.stress),
        )
        assert result == ()

    def test_pre_event_only_returns_pre_event_singleton(self) -> None:
        result = _resolve_active_overlays(
            pre_event_decision=_active(Overlay.pre_event),
            stress_decision=_inactive(Overlay.stress),
        )
        assert result == (Overlay.pre_event,)

    def test_stress_only_returns_stress_singleton(self) -> None:
        result = _resolve_active_overlays(
            pre_event_decision=_inactive(Overlay.pre_event),
            stress_decision=_active(Overlay.stress),
        )
        assert result == (Overlay.stress,)

    def test_both_active_returns_pre_event_then_stress(self) -> None:
        result = _resolve_active_overlays(
            pre_event_decision=_active(Overlay.pre_event),
            stress_decision=_active(Overlay.stress),
        )
        assert result == (Overlay.pre_event, Overlay.stress)


class TestResolveActiveRegime:
    async def test_empty_invocations_returns_regime_normal(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            result = await _resolve_active_regime(session)
        assert result is Regime.normal

    async def test_single_successful_row_returns_its_regime(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            session.add(
                invocation_record_to_row(
                    _make_invocation_record(
                        invocation_id="inv-1",
                        start_at="2026-05-07T14:30:00Z",
                        active_regime="elevated",
                        phase2_completed_at="2026-05-07T14:31:00Z",
                    )
                )
            )
            await session.commit()

        async with async_factory() as session:
            result = await _resolve_active_regime(session)
        assert result is Regime.elevated

    async def test_aborted_row_skipped_and_falls_back_to_prior_successful(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            # Earlier successful invocation
            session.add(
                invocation_record_to_row(
                    _make_invocation_record(
                        invocation_id="inv-old",
                        start_at="2026-05-07T13:00:00Z",
                        active_regime="low_vol",
                        phase2_completed_at="2026-05-07T13:01:00Z",
                    )
                )
            )
            # Later aborted invocation (must be skipped)
            session.add(
                invocation_record_to_row(
                    _make_invocation_record(
                        invocation_id="inv-aborted",
                        start_at="2026-05-07T14:30:00Z",
                        active_regime="crisis",
                        phase2_completed_at=None,
                    )
                )
            )
            await session.commit()

        async with async_factory() as session:
            result = await _resolve_active_regime(session)
        assert result is Regime.low_vol

    async def test_only_aborted_rows_returns_regime_normal(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            session.add(
                invocation_record_to_row(
                    _make_invocation_record(
                        invocation_id="inv-aborted-only",
                        start_at="2026-05-07T14:30:00Z",
                        active_regime="crisis",
                        phase2_completed_at=None,
                    )
                )
            )
            await session.commit()

        async with async_factory() as session:
            result = await _resolve_active_regime(session)
        assert result is Regime.normal


class TestResolveRuntimeDimensions:
    async def test_empty_db_and_inactive_decisions_compose_normal_baseline(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            dimensions = await resolve_runtime_dimensions(
                session,
                firing_trigger=RunType.market_hours_rolling,
                halt_state=None,
                pre_event_decision=_inactive(Overlay.pre_event),
                stress_decision=_inactive(Overlay.stress),
            )

        assert dimensions == RuntimeDimensions(
            active_regime=Regime.normal,
            active_mode=Mode.normal,
            active_overlays=(),
            firing_trigger=RunType.market_hours_rolling,
        )

    async def test_full_composition_threads_each_helper_output(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Prior successful row → elevated; halt state set → halt mode; both
        overlay decisions active → ordered (pre_event, stress)."""
        async with async_factory() as session:
            session.add(
                invocation_record_to_row(
                    _make_invocation_record(
                        invocation_id="inv-prior",
                        start_at="2026-05-07T14:30:00Z",
                        active_regime="elevated",
                        phase2_completed_at="2026-05-07T14:31:00Z",
                    )
                )
            )
            await session.commit()

        halt_state = HaltState(
            daily_halt_active=True,
            cumulative_full_halt_active=False,
            daily_drawdown_pct=0.05,
            daily_drawdown_limit_pct=0.05,
        )

        async with async_factory() as session:
            dimensions = await resolve_runtime_dimensions(
                session,
                firing_trigger=RunType.market_open,
                halt_state=halt_state,
                pre_event_decision=_active(Overlay.pre_event),
                stress_decision=_active(Overlay.stress),
            )

        assert dimensions == RuntimeDimensions(
            active_regime=Regime.elevated,
            active_mode=Mode.halt,
            active_overlays=(Overlay.pre_event, Overlay.stress),
            firing_trigger=RunType.market_open,
        )
