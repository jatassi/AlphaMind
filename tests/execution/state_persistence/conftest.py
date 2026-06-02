"""Shared test substrate for state_persistence tests (ALP-790 hoist).

Houses the duplicated db fixture and the invocation / cash / drawdown
builders and seeds extracted from phase1_write_path, phase2_write_path,
phase1_strategy, phase1_options, and phase1_regt_attribution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.regime import RiskZone
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
)
from alphamind.state.tables.cash_ledger_codec import cash_ledger_record_to_row
from alphamind.state.tables.drawdown_state_codec import drawdown_state_record_to_row

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-aaaa"
_PROCESS_ID = "proc-1"


# ---------------------------------------------------------------------------
# Engine + session fixture (identical copy hoisted from the five phase* files;
# test_six_step_contract.py keeps its alembic-based variant which shadows).
# ---------------------------------------------------------------------------
@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Builders — invocation substrate (hoisted; identical across files except
# for per-file _PROCESS_ID / _INV_ID values used at call sites).
# ---------------------------------------------------------------------------
def _make_state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/pip-freeze",
            "invocation_provenance_root": "/tmp/provenance",
        }
    )


def _make_process_lifetime(
    process_lifetime_id: str = _PROCESS_ID,
    pip_freeze_snapshot_path: str = "/tmp/pip-freeze/proc-1.txt",
) -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=process_lifetime_id,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path=pip_freeze_snapshot_path,
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0",
    )


def _make_invocation_record(
    invocation_id: str = _INV_ID,
    process_lifetime_id: str = _PROCESS_ID,
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=process_lifetime_id,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/provenance/inv/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/provenance/calibration.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


# ---------------------------------------------------------------------------
# Builders — cash / drawdown (general form hoisted; the write_path variant
# with reserved_capital support is a superset, other files' no-arg calls
# produce identical records when reserved=0).
#
# ALP-821 deliberately keeps this execution-side copy rather than importing the
# portfolio_state shared builder: it spans a package boundary (tests/execution ↔
# tests/portfolio_state) and its available_buying_power == current - reserved
# semantics differ from the portfolio_state form (available defaults to current),
# so sharing would couple the two suites for no real payoff.
# ---------------------------------------------------------------------------
def _make_cash_ledger(
    current_cash_usd: float = 100_000.0, *, reserved_capital_usd: float = 0.0
) -> CashLedger:
    return CashLedger(
        current_cash_usd=current_cash_usd,
        settled_cash_usd=current_cash_usd,
        reserved_capital_usd=reserved_capital_usd,
        available_buying_power_usd=current_cash_usd - reserved_capital_usd,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )


def _make_drawdown_state(
    equity_high_water_mark_usd: float = 100_000.0,
) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=equity_high_water_mark_usd,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


# ---------------------------------------------------------------------------
# Seed helpers — cash / drawdown (record form hoisted; callers that passed
# a prebuilt record positionally continue to work; bare calls default to
# make; regt's simpler local form is subsumed).
# ---------------------------------------------------------------------------
async def _seed_cash_ledger(
    factory: async_sessionmaker[AsyncSession],
    record: CashLedger | None = None,
) -> None:
    record = record if record is not None else _make_cash_ledger()
    async with factory() as sess:
        sess.add(cash_ledger_record_to_row(record, last_updated_at=_NOW))
        await sess.commit()


async def _seed_drawdown_state(
    factory: async_sessionmaker[AsyncSession],
    record: DrawdownState | None = None,
) -> None:
    record = record if record is not None else _make_drawdown_state()
    async with factory() as sess:
        sess.add(drawdown_state_record_to_row(record, last_updated_at=_NOW))
        await sess.commit()
