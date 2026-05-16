"""Tests for Q3 options-flow indicators — story 02-distillation-layer/08b.

Cover the deterministic options-derived computations:

- Low-OI volume anomaly (per-contract).
- BTO/STO classification heuristic from snapshot OI deltas.
- Protective vs. speculative tagging via positions presence.
- Pair-trade signature detection across correlated tickers.
- Sector-wide sweep detection.
- ETF IV vs. single-name IV divergence.
- Index hedging vs. sector conviction classification.
- IV-rank baseline state (``atm_iv`` rolling baseline kind).
- Block assembly with ``q3.*`` ids and audience tags.

The tests anchor the threshold values rather than the source — per
``CLAUDE.md`` and ``docs/design/02-distillation-layer/threshold-calibration.md``,
Class A literals reach distillation source only via the loaded
``DistillationConfig``. Tests stand in for the orchestrator that resolves
config in production.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OptionsContracts,
    OptionsContractSnapshots,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory

# Mirror the values that ``config/distillation.yaml`` carries. Per the
# no-magic-numbers audit, distillation source code may not embed these
# literals; tests stand in for the orchestrator that resolves them.
LOW_OI_VOLUME_MULTIPLE = 5.0
LOW_OI_THRESHOLD = 100  # pervasive value; carried for clarity
PAIR_FLOW_SIGMA_THRESHOLD = 1.5
PAIR_CORRELATION_THRESHOLD = 0.6
SECTOR_SWEEP_MIN_NAMES = 3
ETF_IV_DIVERGENCE_SIGMA = 1.0
ATM_IV_HISTORY_DAYS = 252
ATM_IV_MIN_OBSERVATIONS = 60


# ---------------------------------------------------------------------------
# In-memory SQLite scaffolding (mirrors the existing baselines tests)
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


def _add_ticker(
    session: Session,
    ticker: str,
    *,
    asset_role: str = "universe",
    is_active: int = 1,
    avg_daily_volume_shares: int | None = 1_000_000,
) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role=asset_role,
            exchange="NASDAQ",
            avg_daily_volume_shares=avg_daily_volume_shares,
            is_active=is_active,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.flush()


def _add_sector(
    session: Session,
    *,
    ticker: str,
    sector: str,
    sector_etf: str = "XLK",
    domain_researcher: str = "tech-semis",
) -> None:
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=sector,
            domain_researcher=domain_researcher,
            sector_etf=sector_etf,
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_contract(
    session: Session,
    *,
    contract_ticker: str,
    underlying: str,
    contract_type: str = "call",
    strike: float = 100.0,
    expiration: str = "2026-05-15",
) -> None:
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            expiration_date=expiration,
            strike_price=strike,
            contract_type=contract_type,
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-27T00:00:00Z",
            source="test",
        )
    )
    # Flush so the snapshot inserts that follow can resolve the FK without
    # autoflush ordering surprises.
    session.flush()


def _add_snapshot(
    session: Session,
    *,
    contract_ticker: str,
    underlying: str,
    snapshot_ts: str,
    open_interest: int | None,
    volume_today: int | None,
    implied_volatility: float | None = 0.30,
    underlying_price: float = 100.0,
    last_price: float = 1.0,
) -> None:
    session.add(
        OptionsContractSnapshots(
            snapshot_ts=snapshot_ts,
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            open_interest=open_interest,
            volume_today=volume_today,
            last_price=last_price,
            bid=last_price - 0.05,
            ask=last_price + 0.05,
            implied_volatility=implied_volatility,
            delta=0.5,
            gamma=0.05,
            theta=-0.03,
            vega=0.10,
            rho=0.02,
            underlying_price=underlying_price,
            source="test",
            ingested_at=snapshot_ts,
        )
    )


# ---------------------------------------------------------------------------
# Low-OI volume anomaly — ``quant 3b`` + ``options_low_oi_volume_multiple``
# ---------------------------------------------------------------------------


class TestLowOiVolumeAnomaly:
    """Per-contract: ``volume_today >= 5x trailing-avg`` AND ``open_interest < 100``."""

    def test_fires_at_threshold(self, session: Session) -> None:
        from alphamind.distillation.q3 import detect_low_oi_volume_anomalies

        _add_ticker(session, "AAPL")
        _add_contract(session, contract_ticker="O:AAPL260515C00100000", underlying=Symbol("AAPL"))
        # 20 trailing snapshots with volume = 100 each → 20-day avg = 100.
        for day in range(1, 21):
            _add_snapshot(
                session,
                contract_ticker="O:AAPL260515C00100000",
                underlying=Symbol("AAPL"),
                snapshot_ts=f"2026-04-{day:02d}T20:00:00Z",
                open_interest=50,
                volume_today=100,
            )
        # Today's snapshot: volume 500 (5x avg) with OI 50 (< 100).
        _add_snapshot(
            session,
            contract_ticker="O:AAPL260515C00100000",
            underlying=Symbol("AAPL"),
            snapshot_ts="2026-04-25T20:00:00Z",
            open_interest=50,
            volume_today=500,
        )
        session.commit()

        anomalies = detect_low_oi_volume_anomalies(
            session,
            as_of="2026-04-25T20:00:00Z",
            volume_multiple_threshold=LOW_OI_VOLUME_MULTIPLE,
            oi_threshold=LOW_OI_THRESHOLD,
        )

        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.contract_ticker == "O:AAPL260515C00100000"
        assert anomaly.underlying_ticker == "AAPL"
        assert anomaly.volume_today == 500
        assert anomaly.trailing_avg_volume == pytest.approx(100.0)
        assert anomaly.volume_multiple == pytest.approx(5.0)
        assert anomaly.open_interest == 50

    def test_suppressed_when_oi_at_threshold(self, session: Session) -> None:
        """OI >= 100 suppresses the anomaly even with 5x volume."""
        from alphamind.distillation.q3 import detect_low_oi_volume_anomalies

        _add_ticker(session, "AAPL")
        _add_contract(session, contract_ticker="O:AAPL260515C00100000", underlying=Symbol("AAPL"))
        for day in range(1, 21):
            _add_snapshot(
                session,
                contract_ticker="O:AAPL260515C00100000",
                underlying=Symbol("AAPL"),
                snapshot_ts=f"2026-04-{day:02d}T20:00:00Z",
                open_interest=100,  # AT threshold — the spec says "OI < 100"
                volume_today=100,
            )
        _add_snapshot(
            session,
            contract_ticker="O:AAPL260515C00100000",
            underlying=Symbol("AAPL"),
            snapshot_ts="2026-04-25T20:00:00Z",
            open_interest=100,
            volume_today=500,
        )
        session.commit()

        anomalies = detect_low_oi_volume_anomalies(
            session,
            as_of="2026-04-25T20:00:00Z",
            volume_multiple_threshold=LOW_OI_VOLUME_MULTIPLE,
            oi_threshold=LOW_OI_THRESHOLD,
        )

        assert anomalies == []

    def test_suppressed_below_volume_multiple(self, session: Session) -> None:
        from alphamind.distillation.q3 import detect_low_oi_volume_anomalies

        _add_ticker(session, "AAPL")
        _add_contract(session, contract_ticker="O:AAPL260515C00100000", underlying=Symbol("AAPL"))
        for day in range(1, 21):
            _add_snapshot(
                session,
                contract_ticker="O:AAPL260515C00100000",
                underlying=Symbol("AAPL"),
                snapshot_ts=f"2026-04-{day:02d}T20:00:00Z",
                open_interest=50,
                volume_today=100,
            )
        # Today: only 4x trailing avg.
        _add_snapshot(
            session,
            contract_ticker="O:AAPL260515C00100000",
            underlying=Symbol("AAPL"),
            snapshot_ts="2026-04-25T20:00:00Z",
            open_interest=50,
            volume_today=400,
        )
        session.commit()

        anomalies = detect_low_oi_volume_anomalies(
            session,
            as_of="2026-04-25T20:00:00Z",
            volume_multiple_threshold=LOW_OI_VOLUME_MULTIPLE,
            oi_threshold=LOW_OI_THRESHOLD,
        )

        assert anomalies == []


# ---------------------------------------------------------------------------
# BTO/STO classification heuristic — quant 3b, 3f
# ---------------------------------------------------------------------------


class TestBtoStoClassificationHeuristic:
    """Snapshot-OI delta heuristic.

    Per the story: an opening trade increases OI at the strike (volume_today > 0
    with day-over-day OI rise); a closing trade flat-or-decreases OI. Output
    must carry ``attribution_method = "snapshot_oi_delta"``.
    """

    def test_classifies_call_as_bto_when_oi_rises(self, session: Session) -> None:
        from alphamind.distillation.q3 import classify_options_flow

        _add_ticker(session, "AAPL")
        _add_contract(
            session,
            contract_ticker="O:AAPL260515C00100000",
            underlying=Symbol("AAPL"),
            contract_type="call",
        )
        # Prior snapshot: OI 100, no volume.
        _add_snapshot(
            session,
            contract_ticker="O:AAPL260515C00100000",
            underlying=Symbol("AAPL"),
            snapshot_ts="2026-04-24T20:00:00Z",
            open_interest=100,
            volume_today=0,
        )
        # Today: OI 200 (rose by 100), volume 100 — opening trade.
        _add_snapshot(
            session,
            contract_ticker="O:AAPL260515C00100000",
            underlying=Symbol("AAPL"),
            snapshot_ts="2026-04-25T20:00:00Z",
            open_interest=200,
            volume_today=100,
        )
        session.commit()

        flow = classify_options_flow(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-25T20:00:00Z",
        )

        assert "AAPL" in flow
        per_ticker = flow["AAPL"]
        assert per_ticker.attribution_method == "snapshot_oi_delta"
        # 100 contracts of call BTO, 0 STO.
        assert per_ticker.call_bto_volume == 100
        assert per_ticker.call_sto_volume == 0
        assert per_ticker.put_bto_volume == 0
        assert per_ticker.put_sto_volume == 0

    def test_classifies_put_as_sto_when_oi_falls(self, session: Session) -> None:
        from alphamind.distillation.q3 import classify_options_flow

        _add_ticker(session, "AAPL")
        _add_contract(
            session,
            contract_ticker="O:AAPL260515P00090000",
            underlying=Symbol("AAPL"),
            contract_type="put",
            strike=90.0,
        )
        # Prior: OI 200.
        _add_snapshot(
            session,
            contract_ticker="O:AAPL260515P00090000",
            underlying=Symbol("AAPL"),
            snapshot_ts="2026-04-24T20:00:00Z",
            open_interest=200,
            volume_today=0,
        )
        # Today: OI 150 (fell by 50), volume 50 — closing trade (STO/STC).
        _add_snapshot(
            session,
            contract_ticker="O:AAPL260515P00090000",
            underlying=Symbol("AAPL"),
            snapshot_ts="2026-04-25T20:00:00Z",
            open_interest=150,
            volume_today=50,
        )
        session.commit()

        flow = classify_options_flow(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-25T20:00:00Z",
        )

        per_ticker = flow["AAPL"]
        assert per_ticker.put_sto_volume == 50
        assert per_ticker.put_bto_volume == 0

    def test_aggregates_across_strikes_per_ticker(self, session: Session) -> None:
        """Sum opening/closing volumes across all of a ticker's contracts."""
        from alphamind.distillation.q3 import classify_options_flow

        _add_ticker(session, "AAPL")
        # Two call contracts at different strikes.
        for ticker, strike in (
            ("O:AAPL260515C00100000", 100.0),
            ("O:AAPL260515C00110000", 110.0),
        ):
            _add_contract(
                session,
                contract_ticker=ticker,
                underlying=Symbol("AAPL"),
                contract_type="call",
                strike=strike,
            )
            _add_snapshot(
                session,
                contract_ticker=ticker,
                underlying=Symbol("AAPL"),
                snapshot_ts="2026-04-24T20:00:00Z",
                open_interest=100,
                volume_today=0,
            )
            _add_snapshot(
                session,
                contract_ticker=ticker,
                underlying=Symbol("AAPL"),
                snapshot_ts="2026-04-25T20:00:00Z",
                open_interest=200,
                volume_today=100,
            )
        session.commit()

        flow = classify_options_flow(
            session,
            ticker_scope=("AAPL",),
            as_of="2026-04-25T20:00:00Z",
        )

        per_ticker = flow["AAPL"]
        assert per_ticker.call_bto_volume == 200  # 100 + 100 across two strikes


# ---------------------------------------------------------------------------
# Protective vs. speculative tagging — quant 3f
# ---------------------------------------------------------------------------


# Per the story: "if any position table row holds long >= X% of the ticker's
# avg daily volume in shares, classify same-name puts as protective".
# Threshold parameterized so the orchestrator can tune from configuration.
PROTECTIVE_HOLDING_PCT_OF_ADV = 5.0


class TestProtectiveSpeculativeClassification:
    """Per-ticker put-flow classification based on system-held long shares."""

    def test_puts_tagged_protective_when_ticker_in_positions(self, session: Session) -> None:
        from alphamind.distillation.q3 import classify_put_flow_intent

        _add_ticker(session, "AAPL", avg_daily_volume_shares=1_000_000)
        session.commit()

        # System holds 100,000 long AAPL shares == 10% of ADV (above the 5%
        # threshold) → puts on AAPL classified as protective.
        intent = classify_put_flow_intent(
            session,
            ticker_scope=("AAPL",),
            system_long_positions={"AAPL": 100_000},
            protective_holding_pct_of_adv=PROTECTIVE_HOLDING_PCT_OF_ADV,
        )

        assert intent["AAPL"] == "protective"

    def test_puts_tagged_speculative_when_not_held(self, session: Session) -> None:
        from alphamind.distillation.q3 import classify_put_flow_intent

        _add_ticker(session, "AAPL", avg_daily_volume_shares=1_000_000)
        session.commit()

        # System holds nothing on AAPL → puts read as speculative bearish.
        intent = classify_put_flow_intent(
            session,
            ticker_scope=("AAPL",),
            system_long_positions={},
            protective_holding_pct_of_adv=PROTECTIVE_HOLDING_PCT_OF_ADV,
        )

        assert intent["AAPL"] == "speculative"

    def test_puts_speculative_when_holding_below_threshold(self, session: Session) -> None:
        from alphamind.distillation.q3 import classify_put_flow_intent

        _add_ticker(session, "AAPL", avg_daily_volume_shares=1_000_000)
        session.commit()

        # 10,000 shares = 1% of ADV — below the 5% protective threshold.
        intent = classify_put_flow_intent(
            session,
            ticker_scope=("AAPL",),
            system_long_positions={"AAPL": 10_000},
            protective_holding_pct_of_adv=PROTECTIVE_HOLDING_PCT_OF_ADV,
        )

        assert intent["AAPL"] == "speculative"


# ---------------------------------------------------------------------------
# Pair-trade signature detection — quant 3g
# ---------------------------------------------------------------------------


class TestPairTradeSignatureDetection:
    """Cross-ticker correlated opposite-direction BTO flow detection.

    Fires when BTO call flow on ticker A is > 1.5sigma AND BTO put flow on a
    peer ticker B is > 1.5sigma AND their correlation (60-day) is >= 0.6.
    """

    def test_fires_on_correlated_opposite_direction_flow(self, session: Session) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            detect_pair_trade_signatures,
        )

        signatures = detect_pair_trade_signatures(
            flow_zscores={
                "NVDA": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
                "AMD": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
            },
            pair_correlations={("NVDA", "AMD"): 0.75},
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
            correlation_threshold=PAIR_CORRELATION_THRESHOLD,
        )

        assert len(signatures) == 1
        sig = signatures[0]
        assert sig.bullish_leg == "NVDA"
        assert sig.bearish_leg == "AMD"
        assert sig.correlation == pytest.approx(0.75)

    def test_does_not_fire_below_correlation_threshold(self) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            detect_pair_trade_signatures,
        )

        signatures = detect_pair_trade_signatures(
            flow_zscores={
                "NVDA": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
                "AMD": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
            },
            # Correlation 0.55 < 0.6 gate.
            pair_correlations={("NVDA", "AMD"): 0.55},
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
            correlation_threshold=PAIR_CORRELATION_THRESHOLD,
        )

        assert signatures == []

    def test_does_not_fire_below_sigma_threshold(self) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            detect_pair_trade_signatures,
        )

        signatures = detect_pair_trade_signatures(
            flow_zscores={
                # call_bto_z 1.4 < 1.5 sigma threshold.
                "NVDA": FlowZScore(call_bto_z=1.4, put_bto_z=0.0),
                "AMD": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
            },
            pair_correlations={("NVDA", "AMD"): 0.75},
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
            correlation_threshold=PAIR_CORRELATION_THRESHOLD,
        )

        assert signatures == []


# ---------------------------------------------------------------------------
# Sector-wide sweep detection — quant 3g
# ---------------------------------------------------------------------------


class TestSectorWideSweepDetection:
    """At least 3 universe names in the same sector with same-direction
    BTO flow > 1.5sigma within the window emit a ``sector_wide_sweep`` finding.
    """

    def test_fires_at_three_names(self, session: Session) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            detect_sector_wide_sweeps,
        )

        for ticker in ("NVDA", "AMD", "INTC"):
            _add_ticker(session, ticker)
            _add_sector(session, ticker=ticker, sector="tech_semis")
        session.commit()

        sweeps = detect_sector_wide_sweeps(
            session,
            flow_zscores={
                "NVDA": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
                "AMD": FlowZScore(call_bto_z=2.5, put_bto_z=0.0),
                "INTC": FlowZScore(call_bto_z=1.7, put_bto_z=0.0),
            },
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
            min_names=SECTOR_SWEEP_MIN_NAMES,
        )

        assert len(sweeps) == 1
        sweep = sweeps[0]
        assert sweep.sector == "tech_semis"
        assert sweep.direction == "call"
        assert sorted(sweep.tickers) == ["AMD", "INTC", "NVDA"]

    def test_does_not_fire_at_two_names(self, session: Session) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            detect_sector_wide_sweeps,
        )

        for ticker in ("NVDA", "AMD", "INTC"):
            _add_ticker(session, ticker)
            _add_sector(session, ticker=ticker, sector="tech_semis")
        session.commit()

        sweeps = detect_sector_wide_sweeps(
            session,
            flow_zscores={
                "NVDA": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
                "AMD": FlowZScore(call_bto_z=2.5, put_bto_z=0.0),
                "INTC": FlowZScore(call_bto_z=0.5, put_bto_z=0.0),
            },
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
            min_names=SECTOR_SWEEP_MIN_NAMES,
        )

        assert sweeps == []

    def test_separate_sweeps_for_calls_and_puts(self, session: Session) -> None:
        """Same sector can emit two sweeps in different directions
        (e.g. macro hedging on calls and defensive puts simultaneously)."""
        from alphamind.distillation.q3 import (
            FlowZScore,
            detect_sector_wide_sweeps,
        )

        for ticker in ("NVDA", "AMD", "INTC", "JPM", "BAC", "WFC"):
            _add_ticker(session, ticker)
        for ticker in ("NVDA", "AMD", "INTC"):
            _add_sector(session, ticker=ticker, sector="tech_semis")
        for ticker in ("JPM", "BAC", "WFC"):
            _add_sector(
                session,
                ticker=ticker,
                sector="financials",
                sector_etf="XLF",
                domain_researcher="financials",
            )
        session.commit()

        sweeps = detect_sector_wide_sweeps(
            session,
            flow_zscores={
                "NVDA": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
                "AMD": FlowZScore(call_bto_z=2.5, put_bto_z=0.0),
                "INTC": FlowZScore(call_bto_z=1.7, put_bto_z=0.0),
                "JPM": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
                "BAC": FlowZScore(call_bto_z=0.0, put_bto_z=2.5),
                "WFC": FlowZScore(call_bto_z=0.0, put_bto_z=1.7),
            },
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
            min_names=SECTOR_SWEEP_MIN_NAMES,
        )

        sectors_directions = {(s.sector, s.direction) for s in sweeps}
        assert sectors_directions == {
            ("tech_semis", "call"),
            ("financials", "put"),
        }


# ---------------------------------------------------------------------------
# ETF IV vs single-name IV divergence — quant 3h
# ---------------------------------------------------------------------------


class TestEtfIvDivergence:
    """Per-sector ETF IV vs constituent volume-weighted IV divergence.

    Fires when ``etf_iv_zscore`` (vs trailing 60-day baseline of the
    ETF/single-name spread) exceeds the 1sigma threshold.
    """

    def test_fires_at_one_sigma(self) -> None:
        from alphamind.distillation.q3 import compute_etf_iv_divergences

        # Spread mean 0, stdev 0.05; observed spread 0.06 → z = 1.2.
        divergences = compute_etf_iv_divergences(
            sectors={
                "tech_semis": {
                    "etf_ticker": "XLK",
                    "etf_iv": 0.36,
                    "single_name_aggregate_iv": 0.30,
                    "spread_baseline_mean": 0.0,
                    "spread_baseline_stdev": 0.05,
                }
            },
            sigma_threshold=ETF_IV_DIVERGENCE_SIGMA,
        )

        assert len(divergences) == 1
        div = divergences[0]
        assert div.sector == "tech_semis"
        assert div.etf_ticker == "XLK"
        assert div.spread_zscore == pytest.approx(1.2)
        assert div.direction == "etf_leading_names"

    def test_does_not_fire_below_one_sigma(self) -> None:
        from alphamind.distillation.q3 import compute_etf_iv_divergences

        # Spread 0.04 / 0.05 = 0.8 z — under 1.0.
        divergences = compute_etf_iv_divergences(
            sectors={
                "tech_semis": {
                    "etf_ticker": "XLK",
                    "etf_iv": 0.34,
                    "single_name_aggregate_iv": 0.30,
                    "spread_baseline_mean": 0.0,
                    "spread_baseline_stdev": 0.05,
                }
            },
            sigma_threshold=ETF_IV_DIVERGENCE_SIGMA,
        )

        assert divergences == []

    def test_negative_spread_marks_names_leading(self) -> None:
        """When single-name aggregate IV exceeds ETF IV by > 1sigma, the
        direction is ``names_leading_etf``."""
        from alphamind.distillation.q3 import compute_etf_iv_divergences

        divergences = compute_etf_iv_divergences(
            sectors={
                "tech_semis": {
                    "etf_ticker": "XLK",
                    "etf_iv": 0.30,
                    "single_name_aggregate_iv": 0.36,
                    "spread_baseline_mean": 0.0,
                    "spread_baseline_stdev": 0.05,
                }
            },
            sigma_threshold=ETF_IV_DIVERGENCE_SIGMA,
        )

        assert len(divergences) == 1
        assert divergences[0].direction == "names_leading_etf"
        assert divergences[0].spread_zscore == pytest.approx(-1.2)


# ---------------------------------------------------------------------------
# Index hedging vs. sector conviction classification — quant 3h
# ---------------------------------------------------------------------------


class TestIndexVsSectorClassification:
    """When SPY/QQQ put flow and sector-ETF put flow both spike: distinguish."""

    def test_macro_hedging_when_both_spike(self) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            classify_index_vs_sector_flow,
        )

        # Both index ETFs and sector ETFs spike on puts.
        result = classify_index_vs_sector_flow(
            index_flow_zscores={
                "SPY": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
                "QQQ": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
            },
            sector_etf_flow_zscores={
                "XLK": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
                "XLF": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
                "XLE": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
            },
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
        )

        assert result is not None
        assert result.label == "macro_hedging"

    def test_sector_specific_concern_when_only_sector_spikes(self) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            classify_index_vs_sector_flow,
        )

        result = classify_index_vs_sector_flow(
            index_flow_zscores={
                "SPY": FlowZScore(call_bto_z=0.0, put_bto_z=0.5),
                "QQQ": FlowZScore(call_bto_z=0.0, put_bto_z=0.5),
            },
            sector_etf_flow_zscores={
                "XLK": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
                "XLF": FlowZScore(call_bto_z=0.0, put_bto_z=0.0),
                "XLE": FlowZScore(call_bto_z=0.0, put_bto_z=0.0),
            },
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
        )

        assert result is not None
        assert result.label == "sector_specific_concern"

    def test_index_hedging_no_sector_view_when_only_index_spikes(self) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            classify_index_vs_sector_flow,
        )

        result = classify_index_vs_sector_flow(
            index_flow_zscores={
                "SPY": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
                "QQQ": FlowZScore(call_bto_z=0.0, put_bto_z=2.0),
            },
            sector_etf_flow_zscores={
                "XLK": FlowZScore(call_bto_z=0.0, put_bto_z=0.5),
                "XLF": FlowZScore(call_bto_z=0.0, put_bto_z=0.5),
                "XLE": FlowZScore(call_bto_z=0.0, put_bto_z=0.5),
            },
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
        )

        assert result is not None
        assert result.label == "index_hedging_no_sector_view"

    def test_returns_none_when_neither_spikes(self) -> None:
        from alphamind.distillation.q3 import (
            FlowZScore,
            classify_index_vs_sector_flow,
        )

        result = classify_index_vs_sector_flow(
            index_flow_zscores={
                "SPY": FlowZScore(call_bto_z=0.0, put_bto_z=0.5),
            },
            sector_etf_flow_zscores={
                "XLK": FlowZScore(call_bto_z=0.0, put_bto_z=0.5),
            },
            sigma_threshold=PAIR_FLOW_SIGMA_THRESHOLD,
        )

        assert result is None


# ---------------------------------------------------------------------------
# IV-rank baseline state (``atm_iv`` baseline kind) — quant 3a
# ---------------------------------------------------------------------------


def _add_atm_iv_snapshot(
    session: Session,
    *,
    underlying: str,
    snapshot_ts: str,
    implied_volatility: float,
    underlying_price: float = 100.0,
) -> None:
    """Insert an ATM call snapshot for the underlying.

    The IV-rank refresh treats the strike closest to ``underlying_price`` as
    the ATM contract; for the test, a single contract per ticker stands in.
    """
    contract_ticker = f"O:{underlying}260515C00100000"
    existing = session.execute(
        select(OptionsContracts).where(OptionsContracts.contract_ticker == contract_ticker)
    ).scalar_one_or_none()
    if existing is None:
        _add_contract(
            session,
            contract_ticker=contract_ticker,
            underlying=underlying,
            contract_type="call",
            strike=100.0,
        )
    _add_snapshot(
        session,
        contract_ticker=contract_ticker,
        underlying=underlying,
        snapshot_ts=snapshot_ts,
        open_interest=500,
        volume_today=100,
        implied_volatility=implied_volatility,
        underlying_price=underlying_price,
    )


class TestAtmIvBaselineState:
    """``distillation_ticker_baseline`` is extended with ``kind='atm_iv'``."""

    def test_atm_iv_baseline_kind_accepted_by_check_constraint(self, session: Session) -> None:
        """The baseline_kind CHECK constraint admits ``atm_iv``."""
        from alphamind.persistence.models import DistillationTickerBaseline

        _add_ticker(session, "AAPL")
        session.add(
            DistillationTickerBaseline(
                ticker=Symbol("AAPL"),
                baseline_kind="atm_iv",
                as_of="2026-04-25T20:00:00Z",
                mean=0.30,
                stdev=0.05,
                n_observations=60,
                window_days=ATM_IV_HISTORY_DAYS,
                calibration_state="calibrated",
                ingested_at="2026-04-25T20:00:00Z",
            )
        )
        session.commit()

        row = session.execute(
            select(DistillationTickerBaseline).where(
                DistillationTickerBaseline.ticker == "AAPL",
                DistillationTickerBaseline.baseline_kind == "atm_iv",
            )
        ).scalar_one()
        assert row.mean == pytest.approx(0.30)

    def test_refresh_writes_atm_iv_baseline_with_iv_rank(self, session: Session) -> None:
        from alphamind.distillation.q3 import refresh_atm_iv_baselines

        _add_ticker(session, "AAPL")
        session.commit()
        # 60 days of ATM IV ranging from 0.20 to 0.40 → mean ~0.30.
        for day in range(1, 61):
            iv = 0.20 + (day - 1) * (0.20 / 59)  # 0.20 .. 0.40
            _add_atm_iv_snapshot(
                session,
                underlying=Symbol("AAPL"),
                snapshot_ts=_ts_for_day_offset(day),
                implied_volatility=iv,
            )
        session.commit()

        as_of = _ts_for_day_offset(60)
        results = refresh_atm_iv_baselines(
            session,
            ticker_scope=("AAPL",),
            as_of=as_of,
            window_days=ATM_IV_HISTORY_DAYS,
            min_observations=ATM_IV_MIN_OBSERVATIONS,
        )

        cv = results["AAPL"]
        assert cv.state is CalibrationState.CALIBRATED
        # The latest IV was the highest in the trailing window → percentile
        # near 100 (using the "<= value" convention from baselines.py).
        assert cv.value["iv_rank_percentile"] >= 95.0
        assert cv.value["mean"] == pytest.approx(0.30, abs=0.02)

    def test_refresh_marks_bootstrap_below_min_observations(self, session: Session) -> None:
        from alphamind.distillation.q3 import refresh_atm_iv_baselines

        _add_ticker(session, "AAPL")
        session.commit()
        # Only 10 days < 60 minimum.
        for day in range(1, 11):
            _add_atm_iv_snapshot(
                session,
                underlying=Symbol("AAPL"),
                snapshot_ts=_ts_for_day_offset(day),
                implied_volatility=0.30,
            )
        session.commit()

        as_of = _ts_for_day_offset(10)
        results = refresh_atm_iv_baselines(
            session,
            ticker_scope=("AAPL",),
            as_of=as_of,
            window_days=ATM_IV_HISTORY_DAYS,
            min_observations=ATM_IV_MIN_OBSERVATIONS,
        )

        cv = results["AAPL"]
        assert cv.state is CalibrationState.BOOTSTRAP
        assert cv.bootstrap_reason is not None
        assert "atm_iv" in cv.bootstrap_reason


def _ts_for_day_offset(day: int) -> str:
    """Return an ISO 8601 UTC timestamp shifted by ``day`` days from the
    start of the test history (2026-02-01).

    Used by the IV-rank tests to build a contiguous trailing window without
    juggling month boundaries by hand.
    """
    base = datetime(2026, 2, 1, 20, 0, tzinfo=UTC)
    return (base + timedelta(days=day - 1)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Block assembly — ``q3.*`` block_id namespace
# ---------------------------------------------------------------------------


_FRESHNESS = datetime(2026, 4, 25, 20, 0, tzinfo=UTC)


class TestBlockAssembly:
    """The ``q3.*`` ``OutputBlock`` envelopes carry the right audience tags."""

    def test_flow_classification_block_per_sector(self) -> None:
        """One ``q3.flow_classification`` block per sector audience.

        Per the per-ticker convention pinned across 08a-f: the payload is
        ``{"per_ticker": {ticker: {...}, ...}}``; ``audience`` is the
        single sector.
        """
        from alphamind.distillation.q3 import (
            FlowClassificationAssemblyInputs,
            assemble_q3_flow_classification_blocks,
        )

        blocks = assemble_q3_flow_classification_blocks(
            inputs=FlowClassificationAssemblyInputs(
                sector_to_tickers={
                    "tech_semis": ("AAPL", "NVDA"),
                    "financials": ("JPM",),
                },
                per_ticker_payload={
                    "AAPL": {
                        "call_bto_volume": 100,
                        "call_sto_volume": 0,
                        "put_bto_volume": 0,
                        "put_sto_volume": 0,
                        "put_intent": "speculative",
                        "attribution_method": "snapshot_oi_delta",
                    },
                    "NVDA": {
                        "call_bto_volume": 200,
                        "call_sto_volume": 0,
                        "put_bto_volume": 0,
                        "put_sto_volume": 0,
                        "put_intent": "speculative",
                        "attribution_method": "snapshot_oi_delta",
                    },
                    "JPM": {
                        "call_bto_volume": 0,
                        "call_sto_volume": 0,
                        "put_bto_volume": 50,
                        "put_sto_volume": 0,
                        "put_intent": "protective",
                        "attribution_method": "snapshot_oi_delta",
                    },
                },
                sector_to_audience={
                    "tech_semis": OutputAudience.SECTOR_TECH_SEMIS,
                    "financials": OutputAudience.SECTOR_FINANCIALS,
                },
            ),
            freshness_ts=_FRESHNESS,
        )

        ids = sorted(b.block_id for b in blocks)
        assert ids == ["q3.flow_classification", "q3.flow_classification"]
        # Two blocks, each with single-sector audience.
        audiences = {next(iter(b.audience)) for b in blocks}
        assert audiences == {
            OutputAudience.SECTOR_TECH_SEMIS,
            OutputAudience.SECTOR_FINANCIALS,
        }
        for block in blocks:
            assert block.calibration_state is CalibrationState.CALIBRATED
            assert "per_ticker" in block.payload
            per_ticker = block.payload["per_ticker"]
            # Payload tickers are sorted.
            assert list(per_ticker.keys()) == sorted(per_ticker.keys())

    def test_pair_trade_block_carries_multi_sector_audience(self) -> None:
        """A pair-trade signature spans both legs' sector audiences."""
        from alphamind.distillation.q3 import (
            PairTradeSignature,
            assemble_q3_pair_trade_blocks,
        )

        sig = PairTradeSignature(
            bullish_leg="NVDA",
            bearish_leg="JPM",
            bullish_call_bto_z=2.0,
            bearish_put_bto_z=2.0,
            correlation=0.7,
        )
        blocks = assemble_q3_pair_trade_blocks(
            signatures=(sig,),
            ticker_to_sector={"NVDA": "tech_semis", "JPM": "financials"},
            sector_to_audience={
                "tech_semis": OutputAudience.SECTOR_TECH_SEMIS,
                "financials": OutputAudience.SECTOR_FINANCIALS,
            },
            freshness_ts=_FRESHNESS,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q3.pair_trade_signature"
        assert block.audience == frozenset(
            {
                OutputAudience.SECTOR_TECH_SEMIS,
                OutputAudience.SECTOR_FINANCIALS,
            }
        )
        assert block.payload["bullish_leg"] == "NVDA"
        assert block.payload["bearish_leg"] == "JPM"

    def test_index_vs_sector_block_carries_universal_audience(self) -> None:
        """Index-vs-sector spans every sector audience (universal cross-sector)."""
        from alphamind.distillation.q3 import (
            IndexVsSectorClassification,
            assemble_q3_index_vs_sector_block,
        )

        classification = IndexVsSectorClassification(
            label="macro_hedging",
            index_max_put_z=2.0,
            sector_max_put_z=2.0,
        )
        block = assemble_q3_index_vs_sector_block(
            classification=classification,
            sector_audiences=(
                OutputAudience.SECTOR_TECH_SEMIS,
                OutputAudience.SECTOR_FINANCIALS,
                OutputAudience.SECTOR_ENERGY,
            ),
            freshness_ts=_FRESHNESS,
        )

        assert block.block_id == "q3.index_vs_sector_classification"
        assert block.audience == frozenset(
            {
                OutputAudience.SECTOR_TECH_SEMIS,
                OutputAudience.SECTOR_FINANCIALS,
                OutputAudience.SECTOR_ENERGY,
            }
        )
        assert block.payload["label"] == "macro_hedging"

    def test_sector_wide_sweep_block_per_sector(self) -> None:
        """One ``q3.sector_wide_sweep`` block per sweep, sector-scoped audience."""
        from alphamind.distillation.q3 import (
            SectorWideSweep,
            assemble_q3_sector_wide_sweep_blocks,
        )

        sweeps = (
            SectorWideSweep(
                sector="tech_semis",
                direction="call",
                tickers=("AMD", "INTC", "NVDA"),
            ),
        )
        blocks = assemble_q3_sector_wide_sweep_blocks(
            sweeps=sweeps,
            sector_to_audience={
                "tech_semis": OutputAudience.SECTOR_TECH_SEMIS,
            },
            freshness_ts=_FRESHNESS,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q3.sector_wide_sweep"
        assert block.audience == frozenset({OutputAudience.SECTOR_TECH_SEMIS})
        assert block.payload["direction"] == "call"
        assert block.payload["tickers"] == ("AMD", "INTC", "NVDA")

    def test_etf_iv_divergence_block_per_sector(self) -> None:
        """One ``q3.etf_iv_divergence`` block per detected divergence."""
        from alphamind.distillation.q3 import (
            EtfIvDivergence,
            assemble_q3_etf_iv_divergence_blocks,
        )

        divergences = (
            EtfIvDivergence(
                sector="tech_semis",
                etf_ticker="XLK",
                etf_iv=0.36,
                single_name_aggregate_iv=0.30,
                spread=0.06,
                spread_zscore=1.2,
                direction="etf_leading_names",
            ),
        )
        blocks = assemble_q3_etf_iv_divergence_blocks(
            divergences=divergences,
            sector_to_audience={
                "tech_semis": OutputAudience.SECTOR_TECH_SEMIS,
            },
            freshness_ts=_FRESHNESS,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q3.etf_iv_divergence"
        assert block.audience == frozenset({OutputAudience.SECTOR_TECH_SEMIS})
        assert block.payload["direction"] == "etf_leading_names"

    def test_iv_rank_block_per_sector(self) -> None:
        """One ``q3.iv_rank`` block per sector audience."""
        from alphamind.distillation.calibration import CalibratedValue, CalibrationState
        from alphamind.distillation.q3 import assemble_q3_iv_rank_blocks

        per_ticker = {
            "AAPL": CalibratedValue(
                value={
                    "mean": 0.30,
                    "stdev": 0.05,
                    "n_observations": 60,
                    "window_days": 252,
                    "latest_iv": 0.35,
                    "iv_rank_percentile": 95.0,
                },
                state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
            ),
        }
        blocks = assemble_q3_iv_rank_blocks(
            iv_rank_results=per_ticker,
            sector_to_tickers={"tech_semis": ("AAPL",)},
            sector_to_audience={
                "tech_semis": OutputAudience.SECTOR_TECH_SEMIS,
            },
            freshness_ts=_FRESHNESS,
        )

        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q3.iv_rank"
        assert block.audience == frozenset({OutputAudience.SECTOR_TECH_SEMIS})
        assert "per_ticker" in block.payload
        assert "AAPL" in block.payload["per_ticker"]
        assert block.payload["per_ticker"]["AAPL"]["iv_rank_percentile"] == 95.0
