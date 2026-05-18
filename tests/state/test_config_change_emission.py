"""Tests for the ``DISTILLATION_CONFIG_CHANGE`` emission helper (story ALP-100).

Wires the configuration loader into the activity-log substrate so a
``distillation_config_change`` entry is appended inside the open
``InvocationContext`` transaction whenever the resolved ``DistillationConfig``
differs from the prior reload's persisted ``new_hash``. Identical reloads
suppress emission. Per ``state-persistence.md § Activity log entries``, the
entry commits atomically with the parent invocation row, or rolls back with
it on exception.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config.models.distillation import (
    AnomalyDetection,
    DistillationConfig,
    LeadLag,
    LeadLagPair,
    NarrativeLag,
    PersistenceWindows,
    PredictionMarket,
    RegimeClassification,
    RegimeTransition,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.computations.activity_log import (
    compute_distillation_config_hash,
)
from alphamind.portfolio_state.events import (
    DistillationConfigChangeDetail,
    EventSource,
    EventType,
)
from alphamind.state.invocation_context import (
    InvocationContext,
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.invocation_context.config_change import (
    emit_distillation_config_change_entry,
)
from alphamind.state.repository.activity_log_queries import (
    read_intra_invocation_changelog,
    read_most_recent_config_change_new_hash,
)


@pytest.fixture()
async def async_engine_and_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Async engine + factory backed by a fresh on-disk SQLite DB.

    Materializes the full schema and seeds the parent ``process_lifetimes``
    row so any test that opens an ``InvocationContext`` has its FK target.
    """
    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    sync_engine = make_engine(str(db_path))
    with make_session_factory(sync_engine)() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


def _make_invocation_record(
    invocation_id: str = "inv-2026-05-07T14:30:00Z-abcd",
    start_at: str = "2026-05-07T14:30:00Z",
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at=start_at,
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json='["pre-event"]',
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


def _build_distillation_config() -> DistillationConfig:
    return DistillationConfig(
        anomaly_detection=AnomalyDetection(
            volume_anomaly_sigma=2.0,
            price_move_atr_multiple=2.5,
            options_low_oi_volume_multiple=5.0,
            block_trade_min_shares=10_000,
            block_trade_min_notional_usd=1_000_000,
            dark_pool_one_sided_window_minutes=60,
            earnings_revision_cluster_count=3,
            earnings_revision_cluster_days=5,
            macro_surprise_percentile=90,
            funding_stress_component_alert_count=2,
            funding_stress_component_percentile=80,
            market_liquidity_alert_percentile=10,
            news_price_divergence_window_hours=12,
            news_price_divergence_min_articles=5,
        ),
        regime_classification=RegimeClassification(
            regime_low_vol_vix_max=15.0,
            regime_normal_vix_min=15.0,
            regime_normal_vix_max=20.0,
            regime_elevated_vix_min=20.0,
            regime_elevated_vix_max=28.0,
            regime_crisis_vix_min=28.0,
            regime_term_structure_backwardation_threshold=0.0,
            regime_vvix_high_percentile=80,
            regime_vvix_low_percentile=20,
        ),
        regime_transition=RegimeTransition(
            regime_transition_confirmed_invocations=3,
            regime_transition_indicator_agreement_min=3,
            regime_skip_emergency_trigger=True,
        ),
        lead_lag=LeadLag(
            pairs=(
                LeadLagPair(key="credit_to_equity", lead="HYG", lag="SPY"),
                LeadLagPair(key="semis_to_tech", lead="SOXX", lag="QQQ"),
            ),
            lead_lag_funding_to_credit_max_days=4,
            lead_lag_credit_to_equity_max_days=4,
            lead_lag_semis_to_tech_max_days=3,
            lead_lag_financials_to_market_max_days=4,
            lead_lag_commodity_to_energy_equity_max_days=4,
            lead_lag_overdue_lead_sigma=2.0,
        ),
        narrative_lag=NarrativeLag(
            narrative_lag_correlation_shift_sigma=2.0,
            correlation_breakdown_sigma=2.0,
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.05,
            correlation_breakdown_fdr_q=0.05,
            narrative_lag_media_silence_hours=24,
        ),
        persistence_windows=PersistenceWindows(
            volume_baseline_days=20,
            atr_baseline_days=14,
            spread_baseline_days=20,
            correlation_short_days=20,
            correlation_long_days=60,
            sentiment_baseline_days=30,
            sentiment_min_observations=5,
            gap_fill_baseline_days=60,
            gap_fill_min_events=3,
            extended_hours_confirmation_days=30,
            extended_hours_min_events=3,
            prediction_market_history_days=30,
            funding_stress_baseline_days=60,
            market_liquidity_baseline_days=60,
        ),
        prediction_market=PredictionMarket(
            prediction_market_delta_pp_threshold=10.0,
            prediction_market_low_liquidity_volume_min_usd=10_000,
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={},
        ),
    )


def _swap_volume_sigma(config: DistillationConfig, value: float) -> DistillationConfig:
    section = config.anomaly_detection.model_copy(update={"volume_anomaly_sigma": value})
    return config.model_copy(update={"anomaly_detection": section})


_FIXED_TS = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Behaviors
# ---------------------------------------------------------------------------


class TestEmitDistillationConfigChangeEntry:
    async def test_first_ever_reload_persists_baseline_entry(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """No prior reload anywhere → emit a baseline entry (prior_hash=None)."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record()
        config = _build_distillation_config()

        async with InvocationContext(session_factory=factory, record=record) as handle:
            entry = await emit_distillation_config_change_entry(
                handle,
                prior=None,
                new=config,
                timestamp=_FIXED_TS,
                git_sha="a" * 40,
                entry_id="cfg-entry-1",
            )

        assert entry is not None
        assert entry.entry_id == "cfg-entry-1"
        assert entry.invocation_id == record.invocation_id
        assert entry.event_type == EventType.DISTILLATION_CONFIG_CHANGE
        assert entry.source == EventSource.CONFIG_RELOAD
        assert isinstance(entry.detail, DistillationConfigChangeDetail)
        assert entry.detail.prior_hash is None
        assert entry.detail.new_hash == compute_distillation_config_hash(config)
        assert entry.detail.changes == ()
        assert entry.detail.config_file == "config/distillation.yaml"

        # The row landed atomically with the invocation row.
        async with factory() as sess:
            persisted = await read_intra_invocation_changelog(sess, record.invocation_id)
        assert len(persisted) == 1
        assert persisted[0].entry_id == "cfg-entry-1"
        assert persisted[0].detail.new_hash == compute_distillation_config_hash(config)

    async def test_identical_reload_suppresses_emission(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Persisted prior new_hash matches current → no entry persisted, returns None."""
        _, factory = async_engine_and_factory
        config = _build_distillation_config()

        rec1 = _make_invocation_record(invocation_id="inv-1", start_at="2026-05-07T14:30:00Z")
        async with InvocationContext(session_factory=factory, record=rec1) as handle:
            await emit_distillation_config_change_entry(
                handle,
                prior=None,
                new=config,
                timestamp=_FIXED_TS,
                git_sha="a" * 40,
                entry_id="cfg-entry-1",
            )

        rec2 = _make_invocation_record(invocation_id="inv-2", start_at="2026-05-07T15:30:00Z")
        async with InvocationContext(session_factory=factory, record=rec2) as handle:
            entry = await emit_distillation_config_change_entry(
                handle,
                prior=config,
                new=config,
                timestamp=_FIXED_TS,
                git_sha="a" * 40,
                entry_id="cfg-entry-2",
            )

        assert entry is None
        async with factory() as sess:
            second = await read_intra_invocation_changelog(sess, "inv-2")
        assert second == ()

    async def test_changed_reload_persists_diff(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Reload differs from in-process prior → entry carries prior_hash + diff."""
        _, factory = async_engine_and_factory
        prior_config = _build_distillation_config()
        new_config = _swap_volume_sigma(prior_config, 3.5)

        rec1 = _make_invocation_record(invocation_id="inv-1", start_at="2026-05-07T14:30:00Z")
        async with InvocationContext(session_factory=factory, record=rec1) as handle:
            await emit_distillation_config_change_entry(
                handle,
                prior=None,
                new=prior_config,
                timestamp=_FIXED_TS,
                git_sha="a" * 40,
                entry_id="cfg-entry-1",
            )

        rec2 = _make_invocation_record(invocation_id="inv-2", start_at="2026-05-07T15:30:00Z")
        async with InvocationContext(session_factory=factory, record=rec2) as handle:
            entry = await emit_distillation_config_change_entry(
                handle,
                prior=prior_config,
                new=new_config,
                timestamp=_FIXED_TS,
                git_sha="b" * 40,
                entry_id="cfg-entry-2",
            )

        assert entry is not None
        assert entry.detail.prior_hash == compute_distillation_config_hash(prior_config)
        assert entry.detail.new_hash == compute_distillation_config_hash(new_config)
        assert entry.detail.git_sha == "b" * 40
        assert len(entry.detail.changes) == 1
        change = entry.detail.changes[0]
        assert change.key_path == "anomaly_detection.volume_anomaly_sigma"
        assert change.old_value == 2.0
        assert change.new_value == 3.5

    async def test_emission_rolls_back_when_context_raises(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """An exception escaping ``InvocationContext`` rolls back the appended entry."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record(invocation_id="inv-rollback")
        config = _build_distillation_config()

        class _BoomError(RuntimeError):
            pass

        with pytest.raises(_BoomError):
            async with InvocationContext(session_factory=factory, record=record) as handle:
                await emit_distillation_config_change_entry(
                    handle,
                    prior=None,
                    new=config,
                    timestamp=_FIXED_TS,
                    git_sha="a" * 40,
                    entry_id="cfg-rollback",
                )
                raise _BoomError("simulated downstream failure")

        async with factory() as sess:
            new_hash = await read_most_recent_config_change_new_hash(
                sess, "config/distillation.yaml"
            )
        assert new_hash is None

    async def test_suppression_keyed_on_config_file(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Same hash on a *different* config_file does NOT shadow this file's lookup."""
        _, factory = async_engine_and_factory
        config = _build_distillation_config()

        # Persist a baseline for "config/other.yaml" with the same content.
        rec1 = _make_invocation_record(invocation_id="inv-other", start_at="2026-05-07T13:00:00Z")
        async with InvocationContext(session_factory=factory, record=rec1) as handle:
            other = await emit_distillation_config_change_entry(
                handle,
                prior=None,
                new=config,
                timestamp=_FIXED_TS,
                git_sha="a" * 40,
                entry_id="cfg-other",
                config_file="config/other.yaml",
            )
        assert other is not None

        # Now reload "config/distillation.yaml" — the unrelated file's history
        # must NOT trigger suppression.
        rec2 = _make_invocation_record(
            invocation_id="inv-distillation", start_at="2026-05-07T14:00:00Z"
        )
        async with InvocationContext(session_factory=factory, record=rec2) as handle:
            entry = await emit_distillation_config_change_entry(
                handle,
                prior=None,
                new=config,
                timestamp=_FIXED_TS,
                git_sha="a" * 40,
                entry_id="cfg-distillation",
            )

        assert entry is not None
        assert entry.detail.config_file == "config/distillation.yaml"

    async def test_process_restart_with_changed_config_emits_baseline(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Process restart (in-process prior None) with new content → emits new entry.

        Per the contract sub-story, ``build_distillation_config_change_entry``
        treats ``prior=None`` as a baseline — ``prior_hash=None``, ``changes=()``.
        The DB's prior hash differs from the current one, so suppression does
        not kick in.
        """
        _, factory = async_engine_and_factory
        v1 = _build_distillation_config()
        v2 = _swap_volume_sigma(v1, 3.5)

        rec1 = _make_invocation_record(invocation_id="inv-1", start_at="2026-05-07T14:00:00Z")
        async with InvocationContext(session_factory=factory, record=rec1) as handle:
            await emit_distillation_config_change_entry(
                handle,
                prior=None,
                new=v1,
                timestamp=_FIXED_TS,
                git_sha="a" * 40,
                entry_id="cfg-1",
            )

        # Simulate process restart: prior is None even though DB has v1.
        rec2 = _make_invocation_record(invocation_id="inv-2", start_at="2026-05-07T15:00:00Z")
        async with InvocationContext(session_factory=factory, record=rec2) as handle:
            entry = await emit_distillation_config_change_entry(
                handle,
                prior=None,
                new=v2,
                timestamp=_FIXED_TS,
                git_sha="b" * 40,
                entry_id="cfg-2",
            )

        assert entry is not None
        assert entry.detail.new_hash == compute_distillation_config_hash(v2)
        assert entry.detail.prior_hash is None
        assert entry.detail.changes == ()
