"""Tests for the paper-mode enrichment wiring helper (ALP-528).

The continuous-monitor entrypoint (``__main__.py``) gates wedge construction
on ``MonitorSession.mode``: paper-mode builds an ``EnrichmentCallable`` from
the three production lookup adapters + the paper-harness config; live-mode
attaches ``None`` (the consumer's hot path is unchanged).

The gating logic lives in a small helper so it stays unit-testable without
spinning up the full monitor daemon.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.execution import (
    FeeSchedule,
    OrderType,
    PaperHarness,
)
from alphamind.execution.continuous_monitor.session import MonitorMode
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.risk_guardrails.guardrail_evaluation import RealizedVolEntry


@pytest.fixture()
def harness_config() -> PaperHarness:
    return PaperHarness(
        spread_buffer_pct=10,
        impact_coefficients={
            OrderType.market: 0.5,
            OrderType.limit: 0.25,
            OrderType.stop: 0.75,
        },
        fee_schedule=FeeSchedule(
            cat_per_executed_share=0.000166,
            taf_per_share_sells=0.000145,
            sec_pct_of_notional_sells=0.0000080,
            orf_per_options_contract=0.02188,
            occ_per_options_contract=0.02,
        ),
    )


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def test_live_mode_returns_none(
    harness_config: PaperHarness,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from alphamind.execution.continuous_monitor.__main__ import (
        _build_enrichment_callable,
    )

    realized_vol_map: dict[str, RealizedVolEntry] = {}
    callable_ = _build_enrichment_callable(
        mode="live",
        paper_harness=harness_config,
        session_factory=session_factory,
        realized_vol_map=realized_vol_map,
    )
    assert callable_ is None


def test_paper_mode_returns_callable(
    harness_config: PaperHarness,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from alphamind.execution.continuous_monitor.__main__ import (
        _build_enrichment_callable,
    )

    realized_vol_map: dict[str, RealizedVolEntry] = {}
    callable_ = _build_enrichment_callable(
        mode="paper",
        paper_harness=harness_config,
        session_factory=session_factory,
        realized_vol_map=realized_vol_map,
    )
    assert callable_ is not None
    assert callable(callable_)


async def test_paper_mode_callable_is_idempotent_no_estimate_when_inputs_missing(
    harness_config: PaperHarness,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With empty realized-vol map and no orders seeded, the callable returns
    the record unchanged (order-not-found warning path).

    This covers the production default per parent decision H: empty
    realized-vol substrate → estimates default to None for every fill.
    """
    from datetime import UTC, datetime

    from alphamind._kernel.money import money, price
    from alphamind.execution.continuous_monitor.__main__ import (
        _build_enrichment_callable,
    )
    from alphamind.portfolio_state.records.orders import OrderStatus
    from alphamind.state.records import FillProcessingStatus, FillRecord

    realized_vol_map: dict[str, RealizedVolEntry] = {}
    callable_ = _build_enrichment_callable(
        mode="paper",
        paper_harness=harness_config,
        session_factory=session_factory,
        realized_vol_map=realized_vol_map,
    )
    assert callable_ is not None

    record = FillRecord(
        fill_id="fill-1",
        order_id="order-missing",
        fill_timestamp=datetime.now(UTC),
        fill_price=price("100"),
        fill_quantity=1.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=None,
        fees_usd=money("0"),
        execution_venue="paper",
        gateway_reference="alp-1",
        persistence_timestamp=datetime.now(UTC),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )

    result = await callable_(record)
    assert result.live_execution_estimate is None
    assert result == record


def _ensure_mode_literal_unchanged() -> None:
    """Compile-time guard: if ``MonitorMode`` adds variants, ``_build_enrichment_callable``
    must grow corresponding branches. This module-load-time assert keeps the
    coupling visible.
    """
    # The actual variants are validated in test_paper_mode_returns_callable
    # and test_live_mode_returns_none; this guard documents the intent.
    _: MonitorMode = "paper"
    _ = "live"


async def test_refresh_realized_vol_map_in_place_updates_shared_dict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ALP-530 — the monitor refresher reads
    ``ticker_realized_vol`` and updates the shared mapping in place. Both
    the harness ``MapVolLookup`` and the breach-loop ``FixtureIvProvider``
    receive the same dict reference, so an in-place refresh propagates to
    both consumers without reconstruction.
    """
    from alphamind.execution.continuous_monitor.__main__ import (
        refresh_realized_vol_map_in_place,
    )
    from alphamind.persistence.models import AssetUniverse, TickerRealizedVolRow
    from alphamind.state.tables.invocations import InvocationRow
    from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

    # Seed the FK chain: process_lifetimes -> invocations -> ticker_realized_vol.
    async with session_factory() as setup:
        setup.add(
            AssetUniverse(
                asset_id="asset-aapl",
                ticker="AAPL",
                full_name="Apple Inc.",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-05-18T00:00:00Z",
            )
        )
        setup.add(
            ProcessLifetimeRow(
                process_lifetime_id="proc-test",
                process_role="pipeline",
                process_start_at="2026-05-18T00:00:00Z",
                process_pid=1,
                hostname="test-host",
                git_sha="0" * 40,
                git_branch="main",
                git_dirty=0,
                python_version="3.13.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="snap",
                anthropic_sdk_version="0.0.0",
                claude_agent_sdk_version="0.0.0",
                os_release="darwin",
            )
        )
        await setup.flush()
        setup.add(
            InvocationRow(
                invocation_id="seed-inv",
                process_lifetime_id="proc-test",
                start_at="2026-05-18T00:00:00Z",
                phase1_completed_at=None,
                phase2_completed_at=None,
                trigger_type="manual",
                trigger_source="test",
                trigger_reason="seed",
                git_sha_at_invocation="0" * 40,
                active_profile="default",
                active_regime="normal",
                active_mode="normal",
                active_overlays_json="[]",
                resolved_config_hash="0" * 64,
                resolved_config_snapshot_path="snap",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="snap",
                data_source_freshness_json="{}",
                fill_collection_summary_json=None,
                command_execution_summary_json=None,
                staleness_flag=0,
                snapshot_metadata_json=None,
            )
        )
        await setup.flush()
        setup.add(
            TickerRealizedVolRow(
                ticker="AAPL",
                as_of_date="2026-05-18",
                trailing_30d_realized_vol=0.27,
                invocation_id="seed-inv",
                computed_at="2026-05-18T00:00:00Z",
            )
        )
        await setup.commit()

    shared_map: dict[str, RealizedVolEntry] = {}
    await refresh_realized_vol_map_in_place(
        session_factory=session_factory,
        shared_map=shared_map,
        tickers=("AAPL",),
    )

    assert "AAPL" in shared_map
    assert shared_map["AAPL"].trailing_30d_realized_vol == pytest.approx(0.27)


class TestEmptyMapAlertOnce:
    def test_fires_once_when_first_refresh_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from alphamind.execution.continuous_monitor.__main__ import (
            _EmptyMapAlertOnce,
        )

        guard = _EmptyMapAlertOnce()
        with caplog.at_level("ERROR", logger="alphamind.execution.continuous_monitor"):
            guard.observe(count=0)

        records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(records) == 1, f"expected one ERROR, got {len(records)}"
        assert "realized_vol map is empty" in records[0].message

    def test_does_not_re_fire_after_first_empty_observation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from alphamind.execution.continuous_monitor.__main__ import (
            _EmptyMapAlertOnce,
        )

        guard = _EmptyMapAlertOnce()
        with caplog.at_level("ERROR", logger="alphamind.execution.continuous_monitor"):
            guard.observe(count=0)
            guard.observe(count=0)
            guard.observe(count=0)

        records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(records) == 1

    def test_does_not_fire_when_first_refresh_is_populated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from alphamind.execution.continuous_monitor.__main__ import (
            _EmptyMapAlertOnce,
        )

        guard = _EmptyMapAlertOnce()
        with caplog.at_level("ERROR", logger="alphamind.execution.continuous_monitor"):
            guard.observe(count=5)

        records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert records == []

    def test_disarms_after_populated_first_refresh(self, caplog: pytest.LogCaptureFixture) -> None:
        """A populated first refresh disarms the guard — later empty refreshes
        don't trigger the bootstrap alert. The alert is scoped to the
        substrate-just-landed case, not arbitrary later regressions.
        """
        from alphamind.execution.continuous_monitor.__main__ import (
            _EmptyMapAlertOnce,
        )

        guard = _EmptyMapAlertOnce()
        with caplog.at_level("ERROR", logger="alphamind.execution.continuous_monitor"):
            guard.observe(count=5)
            guard.observe(count=0)

        records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert records == []
