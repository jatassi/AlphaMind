"""Tests for the distillation orchestrator entry point — story 02-distillation/12.

These tests exercise :func:`run_external_distillation` end-to-end against an
in-memory SQLite fixture seeded with the minimum tables the orchestrator
touches (asset universe, sector classification, OHLCV bars, macro
observations, news, prediction-market snapshots, event calendar, corporate
actions). The orchestrator composes every prior story (02-11b); these tests
verify the seven-phase contract holds.

Coverage map per the story scope (lines 78-86):

- End-to-end fixture run produces non-empty sector outputs / CR brief / regime label.
- Invocation-archive files written to the expected paths.
- Briefs-table population — brief-store population INSERTs the correlation_regime brief.
- Fault injection: per-category exception propagates without swallowing.
- Determinism: byte-identical outputs across two runs.
- Refresh ordering: refresh failure prevents downstream computation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Pull in the state-persistence tables so ``Base.metadata.create_all`` materializes
# the ``invocations`` and ``process_lifetimes`` rows the briefs FK targets.
import alphamind.state.tables  # noqa: F401
from alphamind._kernel.calibration import CalibrationState
from alphamind.config.models.distillation import (
    DistillationConfig,
    PredictionMarket,
    TrackedCategoryOverride,
)
from alphamind.distillation.aggregation import AnomalySummary, collect_anomalies
from alphamind.distillation.orchestrator import (
    DistillationOutputs,
    _emit_anomaly_activity_log,
    run_external_distillation,
)
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock
from alphamind.distillation.q12_corporate_actions import (
    _emit_divergence_blocks,
    _emit_event_novelty_blocks,
)
from alphamind.persistence.models import (
    CORRELATION_REGIME_BRIEF_KIND,
    AssetUniverse,
    Base,
    Brief,
    DistillationCompositeState,
    MacroObservations,
    OhlcvBars,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
    SectorClassification,
)
from alphamind.portfolio_state.events import (
    DistillationAnomalyFlagDetail,
    EventSource,
    EventType,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_from_row
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow

from .conftest import _build_distillation_config, divergence_detection

# ---------------------------------------------------------------------------
# Fixture seeding helpers
# ---------------------------------------------------------------------------


_SECTOR_TICKERS: dict[str, tuple[str, str, str]] = {
    "NVDA": ("semis", "tech_semis", "SMH"),
    "AMD": ("semis", "tech_semis", "SMH"),
    "AAPL": ("tech", "tech_semis", "XLK"),
    "JPM": ("financials", "financials", "XLF"),
    "BAC": ("financials", "financials", "XLF"),
    "XOM": ("energy", "energy", "XLE"),
    "CVX": ("energy", "energy", "XLE"),
}

# Lead-lag tickers that the orchestrator's Class B pair-lag refresh
# touches. The asset_universe FK on distillation_pair_lag means these
# must exist in asset_universe. The fixture seeds them as auxiliary
# (no sector classification) tickers.
_LEAD_LAG_TICKERS: tuple[str, ...] = ("HYG", "SPY", "SOXX", "QQQ", "XLF", "USO", "XLE")


def _seed_universe_and_sectors(session: Session) -> None:
    for ticker, (alphamind_sector, domain_researcher, etf) in _SECTOR_TICKERS.items():
        session.add(
            AssetUniverse(
                asset_id=f"asset-{ticker.lower()}",
                ticker=ticker,
                full_name=f"{ticker} Holdings",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                avg_daily_volume_shares=1_000_000,
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
        session.add(
            SectorClassification(
                ticker=ticker,
                asset_id=f"asset-{ticker.lower()}",
                alphamind_sector=alphamind_sector,
                domain_researcher=domain_researcher,
                sector_etf=etf,
                classification_source="test",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
    # Lead-lag tickers — only need asset_universe rows (no sector
    # classification) so the pair-lag FK passes.
    for ticker in _LEAD_LAG_TICKERS:
        session.add(
            AssetUniverse(
                asset_id=f"asset-{ticker.lower()}",
                ticker=ticker,
                full_name=f"{ticker} ETF",
                asset_class="etf",
                asset_role="proxy",
                exchange="NYSE",
                avg_daily_volume_shares=10_000_000,
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
    session.commit()


def _seed_ohlcv_for_ticker(session: Session, *, ticker: str, days: int, end: datetime) -> None:
    """Seed ``days`` daily bars ending at ``end`` for ``ticker`` with a mild upward drift."""
    base_price = 100.0
    for offset in range(days, 0, -1):
        bar_date = end - timedelta(days=offset)
        # Add a deterministic perturbation so volume/ATR baselines have variance.
        # The arithmetic uses the offset modulo a small period to ensure
        # observations are not identical across days but remain reproducible.
        perturbation = ((offset * 7) % 11) * 0.5
        price = base_price + perturbation
        volume = 1_000_000 + ((offset * 13) % 17) * 10_000
        session.add(
            OhlcvBars(
                ticker=ticker,
                period_start=bar_date.strftime("%Y-%m-%dT00:00:00Z"),
                timeframe="1d",
                period_end=bar_date.strftime("%Y-%m-%dT23:59:59Z"),
                session="regular",
                adj_open=price,
                adj_high=price + 1.0,
                adj_low=price - 1.0,
                adj_close=price + 0.5,
                adj_volume=volume,
                adj_vwap=price,
                unadj_open=price,
                unadj_high=price + 1.0,
                unadj_low=price - 1.0,
                unadj_close=price + 0.5,
                unadj_volume=volume,
                unadj_vwap=price,
                trade_count=100,
                source="test",
                ingested_at=bar_date.strftime("%Y-%m-%dT00:00:00Z"),
            )
        )
    session.commit()


def _seed_macro_observations(session: Session, *, end: datetime, days: int) -> None:
    """Seed VIX, term-structure, VVIX, realized-vol enabling series for the regime classifier."""
    for offset in range(days, 0, -1):
        ts = end - timedelta(days=offset)
        date_str = ts.strftime("%Y-%m-%d")
        # VIX in the normal band (~17), VVIX moderate, no backwardation.
        for series_id, value in (
            ("VIXCLS", 17.0 + ((offset * 3) % 7) * 0.1),
            ("T10YIE", 2.4),
            ("DGS3MO", 4.5),
            ("DGS2", 4.6),
            ("DGS5", 4.4),
            ("DGS10", 4.3),
            ("DGS30", 4.6),
        ):
            session.add(
                MacroObservations(
                    source="FRED",
                    series_id=series_id,
                    observation_date=date_str,
                    revision_number=0,
                    release_date=date_str,
                    value=value,
                    units="pct",
                    frequency="d",
                    ingested_at=ts.strftime("%Y-%m-%dT00:00:00Z"),
                )
            )
    session.commit()


def _seed_market_liquidity_prior_history(session: Session, *, end: datetime) -> None:
    """Seed 10 prior market-liquidity composite_state rows with non-zero variance.

    Per ALP-575 the composite is a sum of per-component percentile ranks
    (range 0..300, not raw FRED). The fixture seeds STLFSI4 / BAMLC0A0CM
    as absent (resolving to ``0.0`` with empty trailing history → neutral
    score 50.0 each) and VIXCLS with mild day-to-day variation; the
    current normalized sum lands around ~140. Seeded prior values
    230..302 put it below the 10th-percentile boundary so the
    lower-direction alert fires against a real rank rather than the
    no-signal sentinel. Ten rows keep the block in
    :attr:`CalibrationState.ACCUMULATING` (``min_observations`` is 60 in
    the test config).
    """
    for offset in range(10, 0, -1):
        ts = end - timedelta(days=offset + 1)
        session.add(
            DistillationCompositeState(
                composite_kind="market_liquidity",
                as_of=ts.strftime("%Y-%m-%dT00:00:00Z"),
                composite_value=230.0 + offset * 8.0,
                component_breakdown_json="{}",
                percentile_60d=50.0,
                alert_active=0,
                calibration_state="accumulating",
                ingested_at=ts.strftime("%Y-%m-%dT00:00:00Z"),
            )
        )
    session.commit()


@pytest.fixture()
def populated_session(session: Session) -> Session:
    """A session seeded with three sectors, a few weeks of bars, and macro series."""
    _seed_universe_and_sectors(session)
    end = datetime(2026, 4, 25, tzinfo=UTC)
    days = 60
    for ticker in _SECTOR_TICKERS:
        _seed_ohlcv_for_ticker(session, ticker=ticker, days=days, end=end)
    _seed_macro_observations(session, end=end, days=days)
    _seed_market_liquidity_prior_history(session, end=end)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_PROCESS_LIFETIME_ID = "pl-test"


def _seed_invocation_row(session: Session, invocation_id: str) -> None:
    """Insert the process_lifetimes + invocations rows the briefs FK targets.

    The brief-store population's INSERT into ``briefs`` references ``invocations.invocation_id``,
    so an invocation row must exist before the orchestrator runs in tests.
    Idempotent — repeated calls with the same identifiers are no-ops, so the
    helper is safe to invoke twice (e.g., the determinism test runs two
    orchestrator passes against independent engines).
    """
    process_lifetimes = Base.metadata.tables["process_lifetimes"]
    existing = session.execute(
        select(process_lifetimes.c.process_lifetime_id).where(
            process_lifetimes.c.process_lifetime_id == _PROCESS_LIFETIME_ID
        )
    ).scalar_one_or_none()
    if existing is None:
        session.execute(
            process_lifetimes.insert(),
            {
                "process_lifetime_id": _PROCESS_LIFETIME_ID,
                "process_role": "pipeline",
                "process_start_at": "2026-04-25T00:00:00Z",
                "process_pid": 1,
                "hostname": "test",
                "git_sha": "a" * 40,
                "git_branch": "test",
                "git_dirty": 0,
                "python_version": "3.13.0",
                "pip_freeze_hash": "0" * 64,
                "pip_freeze_snapshot_path": "/tmp/pip.txt",
                "anthropic_sdk_version": "0.0.0",
                "claude_agent_sdk_version": "0.0.0",
                "os_release": "test",
            },
        )
    if session.get(InvocationRow, invocation_id) is None:
        session.add(
            InvocationRow(
                invocation_id=invocation_id,
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                start_at="2026-04-25T00:00:00Z",
                fill_collection_completed_at=None,
                command_execution_completed_at=None,
                trigger_type="scheduled",
                trigger_source="test",
                trigger_reason="seed",
                git_sha_at_invocation="a" * 40,
                active_profile="medium",
                active_regime="normal",
                active_mode="normal",
                active_overlays_json="[]",
                resolved_config_hash="0" * 64,
                resolved_config_snapshot_path="/tmp/r.json",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="/tmp/d.json",
                data_source_freshness_json="{}",
                fill_collection_summary_json=None,
                command_execution_summary_json=None,
                staleness_flag=None,
                snapshot_metadata_json=None,
            )
        )
    session.commit()


def _run_orchestrator(
    session: Session,
    *,
    archive_root: Path,
    invocation_id: str = "20260425T120000Z-test",
    provenance_root: Path | None = None,
    emit_anomaly_flags: bool = False,
) -> DistillationOutputs:
    """Helper that drives the orchestrator with the standard fixture config.

    ``provenance_root`` defaults to ``archive_root / "provenance"`` so the
    per-invocation calibration-state snapshot lands inside ``tmp_path``
    rather than the operator's home directory; tests that exercise the
    snapshot path explicitly can override.

    ``emit_anomaly_flags`` opts into the story 04b anomaly activity-log
    emission (off by default to match the replay-harness no-op path).
    """
    _seed_invocation_row(session, invocation_id)
    config = _build_distillation_config()
    as_of = datetime(2026, 4, 25, tzinfo=UTC)
    ticker_scope = tuple(_SECTOR_TICKERS.keys())
    if provenance_root is None:
        provenance_root = archive_root / "provenance"
    return asyncio.run(
        run_external_distillation(
            session=session,
            config=config,
            ticker_scope=ticker_scope,
            as_of=as_of,
            invocation_id=invocation_id,
            archive_root=archive_root,
            provenance_root=provenance_root,
            emit_anomaly_flags=emit_anomaly_flags,
        )
    )


def test_orchestrator_returns_distillation_outputs(
    populated_session: Session, tmp_path: Path
) -> None:
    """End-to-end smoke: orchestrator returns ``DistillationOutputs`` with the documented fields."""
    invocation_id = "20260425T120000Z-test"
    outputs = _run_orchestrator(
        populated_session, archive_root=tmp_path, invocation_id=invocation_id
    )

    assert isinstance(outputs, DistillationOutputs)
    assert outputs.invocation_id == invocation_id
    assert outputs.as_of == datetime(2026, 4, 25, tzinfo=UTC)
    assert set(outputs.sector_outputs) == {
        OutputAudience.SECTOR_TECH_SEMIS,
        OutputAudience.SECTOR_FINANCIALS,
        OutputAudience.SECTOR_ENERGY,
    }
    for sector_output in outputs.sector_outputs.values():
        assert sector_output.text  # non-empty
    assert outputs.correlation_regime_brief.text  # non-empty
    assert "regime_label" in outputs.universal_regime_label


def test_orchestrator_populates_brief_store(populated_session: Session, tmp_path: Path) -> None:
    """Brief-store population: orchestrator INSERTs a ``briefs`` row matching the in-process brief.

    Per story ALP-518: the persisted row is the cross-process backup
    (replay harness, command-center diagnostic); the hot-path
    passthrough through :class:`DistillationOutputs` is preserved so
    in-process consumers see the same brief without a round trip.
    """
    invocation_id = "20260425T120000Z-test"
    outputs = _run_orchestrator(
        populated_session, archive_root=tmp_path, invocation_id=invocation_id
    )
    # In-process passthrough unaffected.
    assert outputs.correlation_regime_brief.text
    assert isinstance(outputs.correlation_regime_brief.reference_index, dict)

    # Persistent row matches.
    rows = (
        populated_session.execute(select(Brief).where(Brief.invocation_id == invocation_id))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    [stored] = rows
    assert stored.brief_kind == CORRELATION_REGIME_BRIEF_KIND
    assert stored.text == outputs.correlation_regime_brief.text
    assert json.loads(stored.reference_index_json) == (
        outputs.correlation_regime_brief.reference_index
    )
    assert stored.created_at  # non-empty ISO timestamp


def test_orchestrator_diagnostic_counts_populated(
    populated_session: Session, tmp_path: Path
) -> None:
    """The diagnostic count fields on :class:`DistillationOutputs` reflect
    per-category-compute / regime-classification output."""
    outputs = _run_orchestrator(populated_session, archive_root=tmp_path)
    assert outputs.total_blocks >= 1  # At minimum, the regime block.
    assert outputs.total_anomalies >= 0
    assert outputs.non_calibrated_block_count >= 0


def test_orchestrator_caps_severity_in_non_calibrated_blocks(
    populated_session: Session, tmp_path: Path
) -> None:
    """No anomaly flag in a non-calibrated block survives at ``investigate_now``.

    The orchestrator routes ``all_blocks`` through
    :func:`alphamind.distillation._severity_cap.cap_blocks_for_calibration`
    before aggregation. Any ``accumulating`` block's flags must be capped
    at ``investigate_if_persists`` or weaker; any ``unavailable`` block's
    flags must be capped at ``note_for_context``. The test also asserts
    that at least one non-calibrated block in the fixture carries an
    anomaly flag, so a future fixture change that drops every such flag
    can't silently turn this assertion into a no-op.
    """
    from alphamind.distillation._calibration_core import CalibrationState

    outputs = _run_orchestrator(populated_session, archive_root=tmp_path)

    non_calibrated_flag_count = 0
    for block in outputs.all_blocks:
        if block.calibration_state is CalibrationState.CALIBRATED:
            continue
        for flag in block.anomaly_flags:
            non_calibrated_flag_count += 1
            assert flag.severity != "investigate_now", (
                f"block {block.block_id} is {block.calibration_state.value}, "
                f"but flag {flag.name} survived at investigate_now"
            )
            if block.calibration_state is CalibrationState.UNAVAILABLE:
                assert flag.severity == "note_for_context", (
                    f"unavailable block {block.block_id} flag {flag.name} "
                    f"should be note_for_context, got {flag.severity}"
                )
    assert non_calibrated_flag_count >= 1, (
        "fixture must produce at least one anomaly flag in a non-calibrated block "
        "so this test exercises the cap; got zero"
    )


def test_orchestrator_persists_ticker_realized_vol(
    populated_session: Session, tmp_path: Path
) -> None:
    """ALP-530 — the orchestrator's per-category-indicator-compute hook writes one row per
    in-scope ticker into ``ticker_realized_vol``. Subsequent reads via
    ``read_realized_vol_map`` see the populated map.
    """
    from alphamind.persistence.models import TickerRealizedVolRow

    invocation_id = "20260425T120000Z-rvol"
    _run_orchestrator(populated_session, archive_root=tmp_path, invocation_id=invocation_id)

    rows = (
        populated_session.execute(
            select(TickerRealizedVolRow).where(TickerRealizedVolRow.invocation_id == invocation_id)
        )
        .scalars()
        .all()
    )
    # The fixture seeds 60 daily bars for each of the seven sector tickers;
    # 30-day realized vol is well-defined for every one of them, so the
    # persister writes one row per ticker.
    assert {r.ticker for r in rows} == set(_SECTOR_TICKERS.keys())
    for row in rows:
        assert row.as_of_date == "2026-04-25"
        assert row.trailing_30d_realized_vol >= 0.0


def test_orchestrator_writes_invocation_archive_files(
    populated_session: Session, tmp_path: Path
) -> None:
    """Invocation-archive write: archive files are written to
    ``<root>/<date>/<invocation>/distillation/``."""
    invocation_id = "20260425T120000Z-test"
    _run_orchestrator(populated_session, archive_root=tmp_path, invocation_id=invocation_id)

    archive_dir = tmp_path / "2026-04-25" / invocation_id / "distillation"
    assert archive_dir.exists()
    assert (archive_dir / "tech_semis_sector.md").exists()
    assert (archive_dir / "financials_sector.md").exists()
    assert (archive_dir / "energy_sector.md").exists()
    assert (archive_dir / "correlation_regime_brief.md").exists()
    assert (archive_dir / "regime.md").exists()
    # The sector documents and the regime archive are non-trivial.
    assert (archive_dir / "tech_semis_sector.md").stat().st_size > 0
    assert (archive_dir / "regime.md").stat().st_size > 0


def test_orchestrator_writes_calibration_state_snapshot(
    populated_session: Session, tmp_path: Path
) -> None:
    """Invocation-archive write: the calibration-state snapshot lands under
    ``provenance_root`` per story 17.

    The snapshot is the deterministic JSON reduction the feedback loop and
    command center calibration-mix panel both consume; the orchestrator's
    invocation-archive-write step invokes the writer alongside the markdown archive write so a
    single fail-closed boundary covers both files.
    """
    invocation_id = "20260425T120000Z-test"
    archive_root = tmp_path / "archive"
    provenance_root = tmp_path / "provenance"
    _run_orchestrator(
        populated_session,
        archive_root=archive_root,
        invocation_id=invocation_id,
        provenance_root=provenance_root,
    )

    snapshot_path = provenance_root / "2026-04-25" / invocation_id / "data_calibration_state.json"
    assert snapshot_path.exists()
    assert snapshot_path.stat().st_size > 0


def _make_independent_populated_session() -> tuple[Engine, Session]:
    """Build a fresh in-memory SQLite + populated session pair.

    Used by tests that need two genuinely independent runs against the
    same fixture data (the determinism test) — sharing one engine causes
    refresh primitives to UPSERT against prior rows rather than running
    a fresh first-invocation path.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    sess = factory()
    _seed_universe_and_sectors(sess)
    end = datetime(2026, 4, 25, tzinfo=UTC)
    for ticker in _SECTOR_TICKERS:
        _seed_ohlcv_for_ticker(sess, ticker=ticker, days=60, end=end)
    _seed_macro_observations(sess, end=end, days=60)
    return eng, sess


def test_orchestrator_fault_injection_propagates_per_category_exception(
    populated_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fault injection: a per-category module exception aborts the invocation.

    The orchestrator must not swallow the exception or replace it with a
    partial result. Per the LLM-agents-uniformly-Critical policy any
    failure in any phase aborts the invocation.
    """
    from alphamind.distillation import orchestrator as orch_mod

    class _InjectedError(RuntimeError):
        """Marker exception so the assertion is unambiguous."""

    def _failing_q12(
        session: Session,
        *,
        config: DistillationConfig,
        as_of: datetime,
    ) -> list[OutputBlock]:
        raise _InjectedError("simulated q12 failure")

    monkeypatch.setattr(orch_mod, "_compute_q12_blocks", _failing_q12)

    with pytest.raises(_InjectedError, match="simulated q12 failure"):
        _run_orchestrator(populated_session, archive_root=tmp_path)


def test_orchestrator_refresh_failure_prevents_downstream_computation(
    populated_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh ordering: a Class B refresh failure prevents any per-category-compute-and-later work.

    The orchestrator must not skip ahead to compute indicators when the
    Class B refresh fails — the indicators read from the state the
    refresh writes, so partial computation would produce stale values.
    """
    from alphamind.distillation import orchestrator as orch_mod

    class _RefreshError(RuntimeError):
        pass

    def _failing_refresh(
        session: Session,
        *,
        config: DistillationConfig,
        ticker_scope: tuple[str, ...],
        contract_scope: tuple[str, ...],
        as_of: datetime,
    ) -> int:
        raise _RefreshError("simulated refresh failure")

    # Sentinel that records whether per-category compute ran.
    category_compute_ran = {"q12": False}

    def _spy_q12(
        session: Session,
        *,
        config: DistillationConfig,
        as_of: datetime,
    ) -> list[OutputBlock]:
        category_compute_ran["q12"] = True
        return []

    monkeypatch.setattr(orch_mod, "_refresh_class_b_state", _failing_refresh)
    monkeypatch.setattr(orch_mod, "_compute_q12_blocks", _spy_q12)

    with pytest.raises(_RefreshError, match="simulated refresh failure"):
        _run_orchestrator(populated_session, archive_root=tmp_path)

    assert category_compute_ran["q12"] is False, (
        "per-category compute ran despite Class B refresh failure"
        " — refresh ordering contract violated"
    )


def test_orchestrator_deterministic_outputs_across_runs(tmp_path: Path) -> None:
    """Determinism: byte-identical outputs across two runs of the same fixture.

    Each run uses its own fresh database so the regime-state INSERT and
    other refresh-primitive writes don't collide. The orchestrator's
    structured-text rendering (story 05) plus the sector / CR assemblers
    (stories 11a / 11b) are deterministic; the orchestrator must not
    introduce any wall-clock or ordering nondeterminism.
    """
    archive_root_a = tmp_path / "run_a"
    archive_root_b = tmp_path / "run_b"
    invocation_id = "20260425T120000Z-test"

    eng_a, sess_a = _make_independent_populated_session()
    try:
        _run_orchestrator(sess_a, archive_root=archive_root_a, invocation_id=invocation_id)
    finally:
        sess_a.close()
        eng_a.dispose()

    eng_b, sess_b = _make_independent_populated_session()
    try:
        _run_orchestrator(sess_b, archive_root=archive_root_b, invocation_id=invocation_id)
    finally:
        sess_b.close()
        eng_b.dispose()

    archive_a = archive_root_a / "2026-04-25" / invocation_id / "distillation"
    archive_b = archive_root_b / "2026-04-25" / invocation_id / "distillation"
    for filename in (
        "tech_semis_sector.md",
        "financials_sector.md",
        "energy_sector.md",
        "correlation_regime_brief.md",
        "regime.md",
    ):
        assert (archive_a / filename).read_bytes() == (archive_b / filename).read_bytes(), (
            f"{filename} differs between runs — determinism contract violated"
        )


def _seed_prediction_market_contracts(session: Session) -> None:
    """Seed two monetary_policy contracts — one above the volume floor, one
    below — plus one election contract that is rejected by category.
    """
    contracts = (
        ("ct-fed-active", "monetary_policy", None, 100_000.0),
        ("ct-fed-low", "monetary_policy", None, 100.0),
        ("ct-elec", "election", None, 100_000.0),
    )
    for contract_id, category, resolution_date, volume in contracts:
        session.add(
            PredictionMarketContracts(
                contract_id=contract_id,
                platform="polymarket",
                description=f"desc {contract_id}",
                category=category,
                resolution_date=resolution_date,
                resolution_outcome=None,
                created_at="2026-01-01T00:00:00Z",
                last_seen_at="2026-04-25T00:00:00Z",
            )
        )
        session.add(
            PredictionMarketSnapshots(
                contract_id=contract_id,
                snapshot_ts="2026-04-25T00:00:00Z",
                yes_probability=0.5,
                volume_24h_usd=volume,
                liquidity_usd=20_000.0,
                bid=0.49,
                ask=0.51,
                ingested_at="2026-04-25T00:00:00Z",
            )
        )
    session.commit()


def test_orchestrator_threads_resolved_contract_scope_to_both_consumers(
    populated_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator must resolve scope ONCE and thread the same tuple to
    ``refresh_contract_history`` (Class B refresh writer) and the qualitative
    per-category-compute loader — splitting would let the writer ingest one set while
    the reader reports on another.

    ALP-487 moved the qualitative reader from a direct
    ``compute_prediction_market_deltas`` call to the
    ``load_qualitative_inputs`` boundary. The scope-flow assertion holds
    at that boundary: the loader consumes ``contract_scope`` and threads
    it onward to the pure compute.
    """
    from alphamind.distillation import baselines as baselines_mod
    from alphamind.distillation import orchestrator as orch_mod
    from alphamind.distillation.qualitative._loaders import (
        load_qualitative_inputs as real_load_qualitative,
    )

    _seed_prediction_market_contracts(populated_session)

    import dataclasses

    config = dataclasses.replace(
        _build_distillation_config(),
        prediction_market=PredictionMarket(
            prediction_market_delta_pp_threshold=10.0,
            prediction_market_low_liquidity_volume_min_usd=10_000,
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={
                "monetary_policy": TrackedCategoryOverride(),
            },
        ).to_domain(),
    )

    captured: dict[str, tuple[str, ...]] = {}
    real_refresh = baselines_mod.refresh_contract_history

    def spy_refresh(session: Session, *, contract_scope, **kwargs):  # type: ignore[no-untyped-def]
        captured["refresh"] = tuple(contract_scope)
        return real_refresh(session, contract_scope=contract_scope, **kwargs)

    def spy_load_qualitative(repo, *, contract_scope, **kwargs):  # type: ignore[no-untyped-def]
        captured["compute"] = tuple(contract_scope)
        return real_load_qualitative(repo, contract_scope=contract_scope, **kwargs)

    monkeypatch.setattr(baselines_mod, "refresh_contract_history", spy_refresh)
    # Both call sites bind via direct-imported symbols on the orchestrator
    # module; patch those bindings, not the source modules.
    monkeypatch.setattr(orch_mod, "refresh_contract_history", spy_refresh)
    monkeypatch.setattr(orch_mod, "load_qualitative_inputs", spy_load_qualitative)

    as_of = datetime(2026, 4, 25, tzinfo=UTC)
    invocation_id = "20260425T120000Z-test"
    _seed_invocation_row(populated_session, invocation_id)
    asyncio.run(
        run_external_distillation(
            session=populated_session,
            config=config,
            ticker_scope=tuple(_SECTOR_TICKERS.keys()),
            as_of=as_of,
            invocation_id=invocation_id,
            archive_root=tmp_path,
            provenance_root=tmp_path / "provenance",
        )
    )

    assert "refresh" in captured, "refresh_contract_history was never invoked"
    assert "compute" in captured, "compute_prediction_market_deltas was never invoked"
    assert captured["refresh"] == captured["compute"], (
        "scope tuple must be identical at both call sites"
    )
    # The above-floor monetary_policy contract is in scope; the low-volume
    # one and the election contract are out.
    assert captured["refresh"] == ("ct-fed-active",)


# ---------------------------------------------------------------------------
# Anomaly activity-log emission (ALP-881 / story 04b)
# ---------------------------------------------------------------------------


def _read_anomaly_entries(session: Session, invocation_id: str) -> list[ActivityLogRow]:
    """Read every ``DISTILLATION_ANOMALY_FLAG`` row for an invocation.

    Queries by the anomaly ``EventType`` + ``invocation_id`` — the shape
    ALP-96's flag-rate reporter consumes.
    """
    return list(
        session.execute(
            select(ActivityLogRow)
            .where(
                ActivityLogRow.event_type == EventType.DISTILLATION_ANOMALY_FLAG.value,
                ActivityLogRow.invocation_id == invocation_id,
            )
            .order_by(ActivityLogRow.entry_id)
        )
        .scalars()
        .all()
    )


def test_orchestrator_emits_one_anomaly_entry_per_flag(
    populated_session: Session, tmp_path: Path
) -> None:
    """N produced flags → N ``DISTILLATION_ANOMALY_FLAG`` rows, each carrying the
    detail-payload fields from the originating flag and the running invocation FK.

    The persisted rows are queried by the anomaly ``EventType`` (the read-back
    shape ALP-96 consumes) and rehydrated into typed entries to assert the
    detail payload round-trips with ``threshold_class`` / ``threshold_key``
    resolved via the taxonomy registry.
    """
    invocation_id = "20260425T120000Z-emit"
    outputs = _run_orchestrator(
        populated_session,
        archive_root=tmp_path,
        invocation_id=invocation_id,
        emit_anomaly_flags=True,
    )

    # The orchestrator's diagnostic count is the source of truth for how many
    # flags the fixture produced this run; the emission must match it 1:1.
    expected_flag_count = sum(len(block.anomaly_flags) for block in outputs.all_blocks)
    assert expected_flag_count >= 1, (
        "fixture must produce at least one anomaly flag so this test exercises emission"
    )

    rows = _read_anomaly_entries(populated_session, invocation_id)
    assert len(rows) == expected_flag_count

    for row in rows:
        assert row.invocation_id == invocation_id
        entry = activity_log_entry_from_row(row)
        assert entry.event_type is EventType.DISTILLATION_ANOMALY_FLAG
        assert entry.source is EventSource.DISTILLATION_ORCHESTRATOR
        detail = entry.detail
        assert isinstance(detail, DistillationAnomalyFlagDetail)
        # Required, non-empty taxonomy fields populated via resolve_flag_taxonomy.
        assert detail.threshold_class
        assert detail.threshold_key

    # Every produced (block_id, flag.name) pair is represented exactly once.
    emitted_keys = {
        (activity_log_entry_from_row(r).detail.block_id, _flag_name_from_row(r)) for r in rows
    }
    produced_keys = {
        (block.block_id, flag.name) for block in outputs.all_blocks for flag in block.anomaly_flags
    }
    assert emitted_keys == produced_keys


def _flag_name_from_row(row: ActivityLogRow) -> str:
    """Recover the originating flag name from the deterministic entry_id suffix."""
    detail = activity_log_entry_from_row(row).detail
    assert isinstance(detail, DistillationAnomalyFlagDetail)
    # entry_id = "{invocation_id}-DISTILLATION_ANOMALY_FLAG-{block_id}-{flag_name}".
    prefix = f"{row.invocation_id}-{EventType.DISTILLATION_ANOMALY_FLAG.value}-{detail.block_id}-"
    assert row.entry_id.startswith(prefix)
    return row.entry_id.removeprefix(prefix)


def test_orchestrator_emits_nothing_without_opt_in(
    populated_session: Session, tmp_path: Path
) -> None:
    """A run with no invocation handle (emit disabled — the replay-harness path)
    emits zero anomaly entries and does not raise."""
    invocation_id = "20260425T120000Z-noemit"
    _run_orchestrator(
        populated_session,
        archive_root=tmp_path,
        invocation_id=invocation_id,
        emit_anomaly_flags=False,
    )
    assert _read_anomaly_entries(populated_session, invocation_id) == []


def test_orchestrator_zero_flags_emits_zero_entries(tmp_path: Path) -> None:
    """A run that produces zero anomaly flags writes zero anomaly entries.

    Uses a session with no seeded market data beyond the universe/sector rows,
    so no per-category producer fires a flag; emission must be a clean no-op
    even with ``emit_anomaly_flags=True``.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    sess = factory()
    try:
        _seed_universe_and_sectors(sess)
        # No OHLCV / macro / liquidity history → no baselines → no flags fire.
        invocation_id = "20260425T120000Z-zero"
        outputs = _run_orchestrator(
            sess,
            archive_root=tmp_path,
            invocation_id=invocation_id,
            emit_anomaly_flags=True,
        )
        produced = sum(len(block.anomaly_flags) for block in outputs.all_blocks)
        assert produced == 0, "fixture unexpectedly produced a flag; the zero-flag path is untested"
        assert _read_anomaly_entries(sess, invocation_id) == []
    finally:
        sess.close()
        eng.dispose()


# ---------------------------------------------------------------------------
# Entry-id collision handling (ALP-934)
# ---------------------------------------------------------------------------


def _per_ticker_volume_summary(*, ticker: str, magnitude: float) -> AnomalySummary:
    """A q1 volume-anomaly summary for one firing ticker in the shared block."""
    return AnomalySummary(
        flag=AnomalyFlag(
            name=f"volume_anomaly:{ticker}",
            magnitude=magnitude,
            severity="investigate_now",
        ),
        source_block_id="q1.volume_anomaly",
        audiences=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        flagged_at=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
    )


def test_emit_multiple_per_ticker_flags_in_one_block_persists_distinct_rows(
    populated_session: Session,
) -> None:
    """N per-ticker q1 flags sharing one block commit as N distinct rows — no PK collision.

    This is the regression for the prod abort (ALP-934): the genesis canary run
    died with ``sqlite3.IntegrityError: UNIQUE constraint failed:
    activity_log.entry_id`` inside the emission ``executemany`` because three
    ``q1.price_move_anomaly`` / five ``q1.volume_anomaly`` flags carried the
    block-level *constant* name, collapsing to one shared ``entry_id`` PK. Driving
    several same-block per-ticker summaries through the real emission path
    (``_emit_anomaly_activity_log`` → ``session.commit()``) must now persist one
    row per ticker. Pre-fix this raised ``IntegrityError`` on commit.
    """
    invocation_id = "20260425T120000Z-collide"
    _seed_invocation_row(populated_session, invocation_id)
    populated_session.commit()

    summaries = [
        _per_ticker_volume_summary(ticker="NVDA", magnitude=1.787),
        _per_ticker_volume_summary(ticker="AMD", magnitude=1.693),
        _per_ticker_volume_summary(ticker="AAPL", magnitude=1.703),
    ]

    written = _emit_anomaly_activity_log(
        populated_session,
        summaries=summaries,
        invocation_id=invocation_id,
        as_of=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
    )
    assert written == len(summaries)

    rows = _read_anomaly_entries(populated_session, invocation_id)
    assert len(rows) == len(summaries)
    # Every row carries a distinct PK and resolves its per-ticker subject.
    assert len({row.entry_id for row in rows}) == len(summaries)
    tickers = set()
    for row in rows:
        detail = activity_log_entry_from_row(row).detail
        assert isinstance(detail, DistillationAnomalyFlagDetail)
        assert detail.block_id == "q1.volume_anomaly"
        tickers.add(detail.ticker)
    assert tickers == {"NVDA", "AMD", "AAPL"}


def test_emit_anomaly_activity_log_dedupes_colliding_entry_id(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Two summaries sharing ``(source_block_id, flag.name)`` persist one row + one WARNING.

    The ALP-934 safety net: identical entry_id keys would violate the
    ``activity_log`` PK and abort the whole distillation transaction. Dedup keeps
    the first occurrence in iteration order and downgrades the loss to one
    observable warning naming the dropped ``block_id`` + ``flag.name``.
    """
    invocation_id = "20260608T133500Z-dedup"
    _seed_invocation_row(session, invocation_id)
    as_of = datetime(2026, 6, 8, 13, 35, tzinfo=UTC)

    def _colliding_summary() -> AnomalySummary:
        return AnomalySummary(
            flag=AnomalyFlag(
                name="q12_event_novelty:sector_financials",
                magnitude=2.0,
                severity="investigate_if_persists",
            ),
            source_block_id="q12.event_novelty",
            audiences=frozenset({OutputAudience.SECTOR_FINANCIALS}),
            flagged_at=as_of,
            calibration_state=CalibrationState.CALIBRATED,
        )

    with caplog.at_level(logging.WARNING, logger="alphamind.distillation.orchestrator"):
        written = _emit_anomaly_activity_log(
            session,
            summaries=[_colliding_summary(), _colliding_summary()],
            invocation_id=invocation_id,
            as_of=as_of,
        )

    assert written == 1
    assert len(_read_anomaly_entries(session, invocation_id)) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "q12.event_novelty" in message
    assert "q12_event_novelty:sector_financials" in message


def test_emit_anomaly_activity_log_persists_all_distinct_q12_subjects(session: Session) -> None:
    """≥2 ETFs + ≥2 audiences firing q12 persist one row per ``(block_id, flag.name)``.

    Regression for the 2026-06-08 abort (ALP-934): driving the real q12 producers
    (each fans a constant-``block_id`` block out per subject) through
    ``collect_anomalies`` and the emit boundary, the per-subject flag-name suffix
    makes every entry_id distinct, so the single ``executemany`` no longer
    violates the ``activity_log`` PK and every flag persists.
    """
    invocation_id = "20260608T133500Z-q12"
    _seed_invocation_row(session, invocation_id)
    as_of = datetime(2026, 6, 8, 13, 35, tzinfo=UTC)

    blocks = [
        *_emit_divergence_blocks(
            detections=[
                divergence_detection("XLK", {"AAPL": 2.5, "MSFT": 2.1}),
                divergence_detection("XLF", {"JPM": 2.4, "BAC": 2.0}),
            ],
            freshness_ts=as_of,
        ),
        *_emit_event_novelty_blocks(
            cadence_per_ticker={
                "AAPL": [{"flag": "unusual_event_cadence"}],
                "JPM": [{"flag": "unusual_event_cadence"}],
            },
            cluster_per_ticker={},
            sector_lookup={"AAPL": "tech", "JPM": "financials"},
            freshness_ts=as_of,
        ),
    ]
    summaries = collect_anomalies(blocks)
    distinct_keys = {(s.source_block_id, s.flag.name) for s in summaries}
    assert len(distinct_keys) == len(summaries) == 4, (
        "fixture must fan out 2 ETFs + 2 audiences with distinct entry_id keys"
    )

    written = _emit_anomaly_activity_log(
        session, summaries=summaries, invocation_id=invocation_id, as_of=as_of
    )

    assert written == 4
    rows = _read_anomaly_entries(session, invocation_id)
    assert len(rows) == 4
