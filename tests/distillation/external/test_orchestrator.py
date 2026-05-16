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
- Briefs-table population (or documented TODO if the schema isn't implemented).
- Fault injection: per-category exception propagates without swallowing.
- Determinism: byte-identical outputs across two runs.
- Refresh ordering: refresh failure prevents downstream computation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

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
    TrackedCategoryOverride,
)
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.orchestrator import (
    DistillationOutputs,
    run_external_distillation,
)
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    MacroObservations,
    OhlcvBars,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
    SectorClassification,
)

# ---------------------------------------------------------------------------
# In-memory SQLite scaffolding — shared across asyncio.to_thread workers
# ---------------------------------------------------------------------------
#
# The orchestrator uses ``asyncio.to_thread`` to drive synchronous DB calls
# off the event loop. SQLite ``:memory:`` databases are per-connection by
# default, so a multi-thread test must pin ``StaticPool`` plus
# ``check_same_thread=False`` to share one underlying connection across
# every worker. Production runs against a file-backed SQLite where each
# thread opens its own connection — the production session factory does
# not need this scaffolding.


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Distillation config builder — produces a complete config from defaults
# ---------------------------------------------------------------------------


def _build_distillation_config() -> DistillationDomainConfig:
    """Return a fully-populated :class:`DistillationDomainConfig` for tests.

    Builds the Pydantic ``DistillationConfig`` (the boundary type) and
    projects it onto the frozen-dataclass mirror that distillation
    consumers take.
    """
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
                LeadLagPair(key="financials_to_market", lead="XLF", lag="SPY"),
                LeadLagPair(key="commodity_to_energy_equity", lead="USO", lag="XLE"),
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
    ).to_domain()


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

# Lead-lag tickers that the orchestrator's Phase 1 pair-lag refresh
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


@pytest.fixture()
def populated_session(session: Session) -> Session:
    """A session seeded with three sectors, a few weeks of bars, and macro series."""
    _seed_universe_and_sectors(session)
    end = datetime(2026, 4, 25, tzinfo=UTC)
    days = 60
    for ticker in _SECTOR_TICKERS:
        _seed_ohlcv_for_ticker(session, ticker=ticker, days=days, end=end)
    _seed_macro_observations(session, end=end, days=days)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run_orchestrator(
    session: Session,
    *,
    archive_root: Path,
    invocation_id: str = "20260425T120000Z-test",
    provenance_root: Path | None = None,
) -> DistillationOutputs:
    """Helper that drives the orchestrator with the standard fixture config.

    ``provenance_root`` defaults to ``archive_root / "provenance"`` so the
    per-invocation calibration-state snapshot lands inside ``tmp_path``
    rather than the operator's home directory; tests that exercise the
    snapshot path explicitly can override.
    """
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


def test_orchestrator_documents_brief_store_todo(
    populated_session: Session, tmp_path: Path
) -> None:
    """Phase 7: orchestrator carries the CR brief in DistillationOutputs without
    requiring the persistent ``briefs`` table (which is not yet implemented).

    The story's Phase 7 contract: when the brief-store schema isn't yet
    implemented, the orchestrator emits a documented TODO (in source) and
    continues — the in-process consumer reads
    :attr:`DistillationOutputs.correlation_regime_brief` directly.
    """
    outputs = _run_orchestrator(populated_session, archive_root=tmp_path)
    assert outputs.correlation_regime_brief.text  # non-empty
    # The brief still carries its reference index for in-process consumers.
    assert isinstance(outputs.correlation_regime_brief.reference_index, dict)


def test_orchestrator_diagnostic_counts_populated(
    populated_session: Session, tmp_path: Path
) -> None:
    """The diagnostic count fields on :class:`DistillationOutputs` reflect Phase-2/3 output."""
    outputs = _run_orchestrator(populated_session, archive_root=tmp_path)
    assert outputs.total_blocks >= 1  # At minimum, the regime block.
    assert outputs.total_anomalies >= 0
    assert outputs.bootstrap_block_count >= 0


def test_orchestrator_writes_invocation_archive_files(
    populated_session: Session, tmp_path: Path
) -> None:
    """Phase 6: archive files are written to ``<root>/<date>/<invocation>/distillation/``."""
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
    """Phase 6: the calibration-state snapshot lands under ``provenance_root`` per story 17.

    The snapshot is the deterministic JSON reduction the feedback loop and
    command center calibration-mix panel both consume; the orchestrator's
    phase 6 invokes the writer alongside the markdown archive write so a
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

    snapshot_path = provenance_root / "invocations" / invocation_id / "data_calibration_state.json"
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
    """Refresh ordering: a Phase 1 failure prevents any Phase 2+ work.

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

    # Sentinel that records whether Phase 2 ran.
    phase_2_ran = {"q12": False}

    def _spy_q12(
        session: Session,
        *,
        config: DistillationConfig,
        as_of: datetime,
    ) -> list[OutputBlock]:
        phase_2_ran["q12"] = True
        return []

    monkeypatch.setattr(orch_mod, "_refresh_class_b_state", _failing_refresh)
    monkeypatch.setattr(orch_mod, "_compute_q12_blocks", _spy_q12)

    with pytest.raises(_RefreshError, match="simulated refresh failure"):
        _run_orchestrator(populated_session, archive_root=tmp_path)

    assert phase_2_ran["q12"] is False, (
        "Phase 2 ran despite Phase 1 refresh failure — refresh ordering contract violated"
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
    ``refresh_contract_history`` (Phase 1 writer) and the qualitative
    Phase 2 loader — splitting would let the writer ingest one set while
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
    asyncio.run(
        run_external_distillation(
            session=populated_session,
            config=config,
            ticker_scope=tuple(_SECTOR_TICKERS.keys()),
            as_of=as_of,
            invocation_id="20260425T120000Z-test",
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
