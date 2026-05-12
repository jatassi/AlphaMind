"""Tests for the Q3 top-level entry point ``assemble_q3_blocks``.

The orchestrator (story 12) drops its ``_placeholder_blocks("q3")`` stub
once this entry point exists. The function composes the per-detection
helpers in :mod:`alphamind.distillation.q3_options` against the loaded
:class:`DistillationConfig` and the asset-universe sector roster.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
)
from alphamind.distillation.output import OutputAudience, OutputBlock
from alphamind.distillation.q3 import assemble_q3_blocks
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OptionsContracts,
    OptionsContractSnapshots,
    SectorClassification,
)

# ---------------------------------------------------------------------------
# In-memory SQLite fixture (mirrors test_orchestrator.py — multi-thread safe)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Config builder
# ---------------------------------------------------------------------------


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
    )


# ---------------------------------------------------------------------------
# Universe seeding helpers
# ---------------------------------------------------------------------------


_SECTOR_TICKERS: dict[str, tuple[str, str, str]] = {
    "NVDA": ("semis", "tech_semis", "SMH"),
    "AMD": ("semis", "tech_semis", "SMH"),
    "JPM": ("financials", "financials", "XLF"),
    "XOM": ("energy", "energy", "XLE"),
}


def _seed_universe(session: Session, *, tickers: tuple[str, ...] | None = None) -> None:
    keys = tickers if tickers is not None else tuple(_SECTOR_TICKERS.keys())
    for ticker in keys:
        alphamind_sector, domain_researcher, etf = _SECTOR_TICKERS[ticker]
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
    session.commit()


_AS_OF = datetime(2026, 4, 25, 20, 0, tzinfo=UTC)


def _seed_atm_iv_history(
    session: Session,
    *,
    ticker: str,
    days: int,
    base_iv: float = 0.30,
) -> None:
    """Seed ``days`` daily ATM-call snapshots so the IV-rank baseline calibrates."""
    contract_ticker = f"O:{ticker}260515C00100000"
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=ticker,
            expiration_date="2026-05-15",
            strike_price=100.0,
            contract_type="call",
            first_seen_at="2026-01-01T00:00:00Z",
            last_seen_at="2026-04-25T20:00:00Z",
            source="test",
        )
    )
    session.flush()
    for day in range(days):
        ts = _AS_OF - timedelta(days=days - 1 - day)
        session.add(
            OptionsContractSnapshots(
                snapshot_ts=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                contract_ticker=contract_ticker,
                underlying_ticker=ticker,
                open_interest=500,
                volume_today=100,
                last_price=1.0,
                bid=0.95,
                ask=1.05,
                implied_volatility=base_iv + 0.001 * day,
                delta=0.5,
                gamma=0.05,
                theta=-0.03,
                vega=0.10,
                rho=0.02,
                underlying_price=100.0,
                source="test",
                ingested_at=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# Acceptance tests
# ---------------------------------------------------------------------------


def test_returns_list_of_output_blocks(session: Session) -> None:
    """The function returns a ``list[OutputBlock]`` (possibly empty under
    sparse fixture data, but never any other type)."""
    _seed_universe(session)

    blocks = assemble_q3_blocks(
        session,
        config=_build_distillation_config(),
        as_of=_AS_OF,
    )

    assert isinstance(blocks, list)
    for block in blocks:
        assert isinstance(block, OutputBlock)


def test_empty_ticker_scope_returns_empty_list(session: Session) -> None:
    """An explicit empty ``ticker_scope`` returns ``[]`` without DB work."""
    _seed_universe(session)

    blocks = assemble_q3_blocks(
        session,
        config=_build_distillation_config(),
        as_of=_AS_OF,
        ticker_scope=(),
    )

    assert blocks == []


def test_per_ticker_payload_sorted_and_audience_pinned(session: Session) -> None:
    """Per-ticker blocks carry tickers sorted; per-sector audience matches
    :data:`DOMAIN_RESEARCHER_BY_AUDIENCE`."""
    _seed_universe(session, tickers=("NVDA", "AMD"))
    # Seed enough ATM-IV history that the IV-rank block calibrates and
    # carries the per-ticker payload for both NVDA and AMD.
    _seed_atm_iv_history(session, ticker="NVDA", days=70)
    _seed_atm_iv_history(session, ticker="AMD", days=70)

    blocks = assemble_q3_blocks(
        session,
        config=_build_distillation_config(),
        as_of=_AS_OF,
    )

    iv_rank_blocks = [b for b in blocks if b.block_id == "q3.iv_rank"]
    assert iv_rank_blocks, "IV-rank block should be emitted with seeded history"
    block = iv_rank_blocks[0]
    # Per-ticker payload sorted ascending.
    per_ticker = block.payload["per_ticker"]
    assert list(per_ticker.keys()) == sorted(per_ticker.keys())
    assert list(per_ticker.keys()) == ["AMD", "NVDA"]
    # Audience matches the documented per-sector mapping.
    assert block.audience == frozenset({OutputAudience.SECTOR_TECH_SEMIS})


def test_pair_correlations_none_skips_pair_trade(session: Session) -> None:
    """When ``pair_correlations`` is ``None``, no ``q3.pair_trade_signature``
    block is emitted — the entry point cannot evaluate pairs without the
    correlation matrix owned by Q7."""
    _seed_universe(session)

    blocks = assemble_q3_blocks(
        session,
        config=_build_distillation_config(),
        as_of=_AS_OF,
        pair_correlations=None,
    )

    pair_blocks = [b for b in blocks if b.block_id == "q3.pair_trade_signature"]
    assert pair_blocks == []


def test_iv_rank_bootstrap_when_history_below_minimum(session: Session) -> None:
    """Per-ticker history shorter than ``atm_iv_min_observations`` flips the
    IV-rank block's calibration to ``BOOTSTRAP`` while still emitting the
    block."""
    from alphamind.distillation.calibration import CalibrationState

    _seed_universe(session, tickers=("NVDA",))
    # Only 10 days of history; the config's minimum is 60.
    _seed_atm_iv_history(session, ticker="NVDA", days=10)

    blocks = assemble_q3_blocks(
        session,
        config=_build_distillation_config(),
        as_of=_AS_OF,
    )

    iv_rank_blocks = [b for b in blocks if b.block_id == "q3.iv_rank"]
    assert iv_rank_blocks, "IV-rank block should be emitted even when bootstrap"
    block = iv_rank_blocks[0]
    assert block.calibration_state is CalibrationState.BOOTSTRAP
    assert block.bootstrap_reason is not None
    assert "atm_iv" in block.bootstrap_reason


def test_multi_sector_block_carries_audience_union(session: Session) -> None:
    """Cross-sector ``q3.index_vs_sector_classification`` blocks carry the
    union of every sector audience present in this invocation."""
    _seed_universe(session)  # NVDA + AMD (tech_semis), JPM (financials), XOM (energy).

    # Index ETF + every sector-ETF: spike both side's put-BTO so the
    # classifier emits ``macro_hedging``. The simplest seeding reuses the
    # same contract layout per ETF; the entry point's z-score helper
    # produces a high z when today's volume dwarfs the trailing window.
    _seed_universe(session, tickers=())  # idempotent; no-op if rerun
    for etf in ("SPY", "XLK", "XLF", "XLE"):
        # Add each ETF to asset_universe so the BTO-z lookup can resolve.
        session.add(
            AssetUniverse(
                asset_id=f"asset-{etf.lower()}",
                ticker=etf,
                full_name=f"{etf} ETF",
                asset_class="etf",
                asset_role="proxy",
                exchange="NYSE",
                avg_daily_volume_shares=10_000_000,
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
        _seed_put_bto_spike(session, ticker=etf)
    session.commit()

    blocks = assemble_q3_blocks(
        session,
        config=_build_distillation_config(),
        as_of=_AS_OF,
        ticker_scope=("NVDA", "AMD", "JPM", "XOM", "SPY", "XLK", "XLF", "XLE"),
    )

    cross_sector_blocks = [b for b in blocks if b.block_id == "q3.index_vs_sector_classification"]
    assert cross_sector_blocks, "macro hedging fixture should fire the cross-sector block"
    audience = cross_sector_blocks[0].audience
    # Audience union spans every sector audience present.
    assert OutputAudience.SECTOR_TECH_SEMIS in audience
    assert OutputAudience.SECTOR_FINANCIALS in audience
    assert OutputAudience.SECTOR_ENERGY in audience


def _seed_put_bto_spike(session: Session, *, ticker: str) -> None:
    """Seed prior put-OI history of zero (so today's spike reads as huge BTO)
    and a today-dated put-BTO of 1000 contracts.

    The BTO-z helper computes ``(today_value - mean_history) / stdev_history``
    against the per-day window. To produce a z-score above the configured
    1.5sigma threshold, we need a non-trivial trailing window (length ≥ 2) with
    finite variance and a today value that dominates it.
    """
    contract_ticker = f"O:{ticker}260515P00100000"
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=ticker,
            expiration_date="2026-05-15",
            strike_price=100.0,
            contract_type="put",
            first_seen_at="2026-01-01T00:00:00Z",
            last_seen_at="2026-04-25T20:00:00Z",
            source="test",
        )
    )
    session.flush()
    # Trailing window: 5 prior days with monotonically rising OI by 1 each
    # day so each prior day registers a small BTO of volume=10. That gives
    # mean ≈ 10, stdev = 0, which the z helper guards as 0.0 — to get a
    # meaningful stdev we vary the daily volume.
    prior_volumes = [10, 12, 9, 11, 10]
    cumulative_oi = 100
    for offset, vol in enumerate(prior_volumes):
        cumulative_oi += 1
        ts = _AS_OF - timedelta(days=len(prior_volumes) - offset)
        session.add(
            OptionsContractSnapshots(
                snapshot_ts=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                contract_ticker=contract_ticker,
                underlying_ticker=ticker,
                open_interest=cumulative_oi,
                volume_today=vol,
                last_price=1.0,
                bid=0.95,
                ask=1.05,
                implied_volatility=0.30,
                delta=-0.5,
                gamma=0.05,
                theta=-0.03,
                vega=0.10,
                rho=0.02,
                underlying_price=100.0,
                source="test",
                ingested_at=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
    # Today's snapshot: huge BTO spike (volume 1000, OI rises by 1000).
    cumulative_oi += 1000
    session.add(
        OptionsContractSnapshots(
            snapshot_ts=_AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ"),
            contract_ticker=contract_ticker,
            underlying_ticker=ticker,
            open_interest=cumulative_oi,
            volume_today=1000,
            last_price=1.0,
            bid=0.95,
            ask=1.05,
            implied_volatility=0.30,
            delta=-0.5,
            gamma=0.05,
            theta=-0.03,
            vega=0.10,
            rho=0.02,
            underlying_price=100.0,
            source="test",
            ingested_at=_AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


def test_two_calls_produce_byte_identical_lists(session: Session) -> None:
    """Determinism: two ``assemble_q3_blocks`` calls against the same fixture
    produce byte-identical block lists. Required for the invocation
    archive's deterministic-diff property."""
    from alphamind.distillation.output import format_block

    _seed_universe(session, tickers=("NVDA", "AMD"))
    _seed_atm_iv_history(session, ticker="NVDA", days=70)
    _seed_atm_iv_history(session, ticker="AMD", days=70)

    config = _build_distillation_config()
    first = assemble_q3_blocks(session, config=config, as_of=_AS_OF)
    second = assemble_q3_blocks(session, config=config, as_of=_AS_OF)

    assert len(first) == len(second)
    assert [block.block_id for block in first] == [block.block_id for block in second]
    assert [block.audience for block in first] == [block.audience for block in second]
    # Byte-identical when rendered.
    assert "\n".join(format_block(b) for b in first) == "\n".join(format_block(b) for b in second)
