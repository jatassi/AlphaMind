"""Tests for ``q7_cross_asset.assemble_q7_blocks`` — story 14a follow-up.

Cover the top-level entry point that lets the orchestrator drop its
``_placeholder_blocks("q7")`` stub. Acceptance criteria mirror the
dispatch checklist:

- Returns a list of ``OutputBlock`` instances.
- Audience routing per story 08d: cross-asset
  correlation/rotation/lead-lag/regime-change/narrative-lag default to
  ``CORRELATION_REGIME_BRIEF``; breadth and intermarket carry
  ``UNIVERSAL_BROADCAST`` as well.
- Pair-correlation helper returns a dict keyed by ``(ticker_a, ticker_b)``
  tuples for use by q3's pair-trade-signature detection.
- Empty ``ticker_scope`` returns ``[]``.
- Bootstrap path: insufficient observation history produces blocks tagged
  ``BOOTSTRAP`` rather than crashing.
- Determinism: two calls byte-identical.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

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
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience, OutputBlock, format_block
from alphamind.distillation.q7 import (
    assemble_q7_blocks,
    compute_pair_correlations,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationPairLag,
    MacroObservations,
    OhlcvBars,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def _build_config() -> DistillationDomainConfig:
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
    ).to_domain()


# Universe roster: a sector-classified leg per AlphaMind sector plus the
# cross-asset ETFs and macro proxies the q7 compute functions read.
_SECTOR_TICKERS: dict[str, tuple[str, str]] = {
    "AAPL": ("tech", "XLK"),
    "MSFT": ("tech", "XLK"),
    "NVDA": ("semis", "SMH"),
    "AMD": ("semis", "SMH"),
    "JPM": ("financials", "XLF"),
    "BAC": ("financials", "XLF"),
    "XOM": ("energy", "XLE"),
    "CVX": ("energy", "XLE"),
}

# Cross-asset ETFs and proxies. The q7 compute functions read these by
# fixed ticker; their AssetUniverse rows must exist so DB foreign keys
# resolve, but no SectorClassification is needed.
_CROSS_ASSET_TICKERS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "TLT",
    "GLD",
    "XLE",
    "XLK",
    "XLF",
    "SMH",
    "IWM",
    "HYG",
    "SOXX",
    "USO",
)


def _add_universe_ticker(
    session: Session,
    *,
    ticker: str,
    asset_class: str = "equity",
    asset_role: str = "universe",
) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class=asset_class,
            asset_role=asset_role,
            exchange="NYSE",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_sector_classification(session: Session, *, ticker: str, sector: str, etf: str) -> None:
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=sector,
            domain_researcher=(
                "tech_semis"
                if sector in ("tech", "semis")
                else ("financials" if sector == "financials" else "energy")
            ),
            sector_etf=etf,
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_close(session: Session, *, ticker: str, period_start: str, close: float) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=close,
            adj_high=close,
            adj_low=close,
            adj_close=close,
            adj_volume=1_000_000,
            adj_vwap=close,
            unadj_open=close,
            unadj_high=close,
            unadj_low=close,
            unadj_close=close,
            unadj_volume=1_000_000,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


def _seed_path(
    session: Session,
    *,
    ticker: str,
    closes: list[float],
    start_day: datetime,
) -> None:
    for i, close in enumerate(closes):
        ts = (start_day + timedelta(days=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_close(session, ticker=ticker, period_start=ts, close=close)


def _seed_macro(
    session: Session,
    *,
    series_id: str,
    source: str,
    values: list[float],
    start_day: datetime,
) -> None:
    for i, v in enumerate(values):
        observation_date = (start_day + timedelta(days=i)).strftime("%Y-%m-%d")
        session.add(
            MacroObservations(
                source=source,
                series_id=series_id,
                observation_date=observation_date,
                revision_number=0,
                release_date=observation_date,
                value=v,
                units="pct",
                frequency="d",
                ingested_at="2026-04-26T00:00:00Z",
            )
        )


def _seed_universe(session: Session, as_of: datetime, *, n_days: int = 61) -> None:
    """Seed enough universe rows + price history for assemble_q7_blocks to run."""
    start_day = as_of - timedelta(days=n_days - 1)
    flat = [100.0 + i * 0.5 for i in range(n_days)]

    for ticker, (sector, etf) in _SECTOR_TICKERS.items():
        _add_universe_ticker(session, ticker=ticker)
        _add_sector_classification(session, ticker=ticker, sector=sector, etf=etf)
        _seed_path(session, ticker=ticker, closes=flat, start_day=start_day)

    for ticker in _CROSS_ASSET_TICKERS:
        _add_universe_ticker(session, ticker=ticker, asset_class="etf", asset_role="benchmark")
        _seed_path(session, ticker=ticker, closes=flat, start_day=start_day)

    _seed_macro(
        session,
        series_id="DFII10",
        source="fred",
        values=[1.5] * n_days,
        start_day=start_day,
    )
    _seed_macro(
        session,
        series_id="DCOILWTICO",
        source="fred",
        values=[70.0] * n_days,
        start_day=start_day,
    )
    _seed_macro(
        session,
        series_id="VIXCLS",
        source="fred",
        values=[15.0] * n_days,
        start_day=start_day,
    )
    # Persisted lead-lag rows so the lead-lag block does not always tag
    # bootstrap on the calibrated path.
    as_of_iso = as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for lead, lag in (("HYG", "SPY"), ("SOXX", "QQQ"), ("XLF", "SPY"), ("USO", "XLE")):
        session.add(
            DistillationPairLag(
                lead_ticker=lead,
                lag_ticker=lag,
                as_of=as_of_iso,
                lead_lag_days_estimate=1.0,
                n_pair_events=30,
                last_overdue_flag=0,
                calibration_state="calibrated",
                ingested_at=as_of_iso,
            )
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAssembleQ7Blocks:
    def test_returns_list_of_output_blocks(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        _seed_universe(session, as_of)
        session.commit()

        blocks = assemble_q7_blocks(session, config=_build_config(), as_of=as_of)

        assert isinstance(blocks, list)
        assert blocks, "expected at least one Q7 block"
        for block in blocks:
            assert isinstance(block, OutputBlock)
            assert block.block_id.startswith("q7.")

    def test_audience_routing_matches_story_spec(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        _seed_universe(session, as_of)
        session.commit()

        blocks = assemble_q7_blocks(session, config=_build_config(), as_of=as_of)

        # Spec routing: correlation/rotation/lead-lag/regime-change/narrative-lag
        # default to CR_BRIEF only; breadth + intermarket carry UNIVERSAL_BROADCAST.
        cr_only_prefixes = (
            "q7.intra_sector_correlation",
            "q7.cross_sector_rotation",
            "q7.lead_lag.",
            "q7.correlation_breakdown.",
            "q7.narrative_lag",
        )
        broadcast_prefixes = ("q7.breadth_internals", "q7.intermarket_regime.")

        cr_only_seen = False
        broadcast_seen = False
        for block in blocks:
            if any(block.block_id.startswith(p) for p in cr_only_prefixes):
                assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
                assert OutputAudience.UNIVERSAL_BROADCAST not in block.audience
                cr_only_seen = True
            elif any(block.block_id.startswith(p) for p in broadcast_prefixes):
                assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
                assert OutputAudience.UNIVERSAL_BROADCAST in block.audience
                broadcast_seen = True
            else:
                pytest.fail(f"unexpected block_id: {block.block_id}")
        assert cr_only_seen
        assert broadcast_seen

    def test_explicit_ticker_scope_constrains_universe(self, session: Session) -> None:
        """When ``ticker_scope`` is provided, sector rosters honor it."""
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        _seed_universe(session, as_of)
        session.commit()

        blocks = assemble_q7_blocks(
            session,
            config=_build_config(),
            as_of=as_of,
            ticker_scope=("AAPL", "MSFT"),
        )

        intra_sector_blocks = [
            b for b in blocks if b.block_id.startswith("q7.intra_sector_correlation.")
        ]
        # Only the ``tech`` sector is represented in the scope.
        assert len(intra_sector_blocks) == 1
        only = intra_sector_blocks[0]
        matrix = only.payload["short_window"]["correlation_matrix"]
        assert sorted(matrix) == ["AAPL", "MSFT"]

    def test_empty_ticker_scope_returns_empty_list(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        _seed_universe(session, as_of)
        session.commit()

        blocks = assemble_q7_blocks(session, config=_build_config(), as_of=as_of, ticker_scope=())
        assert blocks == []

    def test_bootstrap_when_observation_history_insufficient(self, session: Session) -> None:
        """Insufficient observation history produces ``BOOTSTRAP`` blocks, not crashes."""
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        # Seed only a handful of days — well below the long-window threshold.
        _seed_universe(session, as_of, n_days=5)
        session.commit()

        blocks = assemble_q7_blocks(session, config=_build_config(), as_of=as_of)

        assert blocks, "expected blocks even on the sub-calibrated path"
        non_calibrated_blocks = [
            b for b in blocks if b.calibration_state is not CalibrationState.CALIBRATED
        ]
        assert non_calibrated_blocks, (
            "expected at least one non-calibrated block on insufficient history"
        )

    def test_determinism_across_repeated_invocations(self, session: Session) -> None:
        """Two consecutive calls produce byte-identical rendered output."""
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        _seed_universe(session, as_of)
        session.commit()

        config = _build_config()
        first = assemble_q7_blocks(session, config=config, as_of=as_of)
        second = assemble_q7_blocks(session, config=config, as_of=as_of)

        first_text = "".join(format_block(b) for b in first)
        second_text = "".join(format_block(b) for b in second)
        assert first_text == second_text


class TestComputePairCorrelations:
    def test_pair_correlation_helper_returns_tuple_keyed_dict(self, session: Session) -> None:
        """``compute_pair_correlations`` returns a dict keyed by ``(a, b)`` tuples."""
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        _seed_universe(session, as_of)
        session.commit()

        pair_correlations = compute_pair_correlations(
            session,
            ticker_scope=("AAPL", "MSFT", "NVDA"),
            as_of=as_of,
            window_days=20,
        )

        assert isinstance(pair_correlations, dict)
        # Symmetric ordering: (a, b) and (b, a) both present so the q3
        # pair-trade-signature scan hits both orderings.
        keys = set(pair_correlations)
        assert ("AAPL", "MSFT") in keys
        assert ("MSFT", "AAPL") in keys
        # Tuple keys; values are floats in [-1, 1].
        for key, value in pair_correlations.items():
            assert isinstance(key, tuple) and len(key) == 2
            assert -1.0 <= value <= 1.0

    def test_pair_correlations_omit_self_pairs(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        _seed_universe(session, as_of)
        session.commit()

        pair_correlations = compute_pair_correlations(
            session,
            ticker_scope=("AAPL", "MSFT"),
            as_of=as_of,
            window_days=20,
        )
        for a, b in pair_correlations:
            assert a != b
