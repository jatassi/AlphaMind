"""Tests for prediction-market deltas — story 02-distillation-layer/08f.

Cover ``compute_prediction_market_deltas``: inter-invocation deltas,
exact-boundary delta-threshold gating, low-liquidity tagging, trailing
30-day history, cross-platform liquidity-weighted normalization, audience
:attr:`OutputAudience.UNIVERSAL_BROADCAST`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.qualitative_derived import (
    compute_prediction_market_deltas,
)
from alphamind.persistence.models import (
    Base,
    DistillationContractHistory,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory

DELTA_PP_THRESHOLD = 5.0
LOW_LIQUIDITY_VOLUME_MIN_USD = 10_000.0
HISTORY_DAYS = 30


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


def _add_contract(
    session: Session,
    *,
    contract_id: str,
    platform: str = "polymarket",
    description: str = "Will the Fed hold rates in January?",
    category: str = "monetary_policy",
) -> None:
    session.add(
        PredictionMarketContracts(
            contract_id=contract_id,
            platform=platform,
            description=description,
            category=category,
            created_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
        )
    )
    session.flush()


def _add_history_row(
    session: Session,
    *,
    contract_id: str,
    snapshot_ts: str,
    yes_probability: float,
    delta_pp: float = 0.0,
    liquidity_usd: float = 50_000.0,
) -> None:
    session.add(
        DistillationContractHistory(
            contract_id=contract_id,
            snapshot_ts=snapshot_ts,
            yes_probability=yes_probability,
            delta_pp_since_prior=delta_pp,
            liquidity_usd=liquidity_usd,
            calibration_state=CalibrationState.CALIBRATED.value,
            ingested_at=snapshot_ts,
        )
    )


def _add_snapshot(
    session: Session,
    *,
    contract_id: str,
    snapshot_ts: str,
    yes_probability: float = 0.5,
    volume_24h_usd: float = 50_000.0,
    liquidity_usd: float = 50_000.0,
) -> None:
    session.add(
        PredictionMarketSnapshots(
            contract_id=contract_id,
            snapshot_ts=snapshot_ts,
            yes_probability=yes_probability,
            volume_24h_usd=volume_24h_usd,
            liquidity_usd=liquidity_usd,
            bid=None,
            ask=None,
            ingested_at=snapshot_ts,
        )
    )


# ---------------------------------------------------------------------------
# Delta threshold — exactly 5.0pp fires; 4.99pp does not
# ---------------------------------------------------------------------------


class TestPredictionMarketDeltaThreshold:
    def test_delta_at_exactly_5_pp_fires_anomaly(self, session: Session) -> None:
        _add_contract(session, contract_id="pm-001")
        _add_history_row(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.55,
            delta_pp=5.0,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            volume_24h_usd=50_000.0,
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        delta_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_delta"]
        assert len(delta_blocks) == 1
        block = delta_blocks[0]
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience
        per_contract = block.payload["per_contract"]
        assert per_contract["pm-001"]["delta_anomaly"] is True
        # Exactly one anomaly flag
        assert len(block.anomaly_flags) == 1
        flag = block.anomaly_flags[0]
        assert flag.name == "prediction_market_delta"
        assert flag.severity == "investigate_now"
        assert flag.magnitude == pytest.approx(5.0)

    def test_delta_at_4_99_pp_suppresses_anomaly(self, session: Session) -> None:
        _add_contract(session, contract_id="pm-001")
        _add_history_row(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.55,
            delta_pp=4.99,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            volume_24h_usd=50_000.0,
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        delta_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_delta"]
        per_contract = delta_blocks[0].payload["per_contract"]
        assert per_contract["pm-001"]["delta_anomaly"] is False
        # No anomaly flag
        assert len(delta_blocks[0].anomaly_flags) == 0


# ---------------------------------------------------------------------------
# Low-liquidity tagging — exactly $10,000 fires; $10,001 suppresses
# ---------------------------------------------------------------------------


class TestPredictionMarketLowLiquidityTagging:
    def test_low_liquidity_tag_fires_at_exactly_10000_volume(self, session: Session) -> None:
        _add_contract(session, contract_id="pm-001")
        _add_history_row(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.55,
            delta_pp=0.0,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            volume_24h_usd=10_000.0,
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        delta_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_delta"]
        per_contract = delta_blocks[0].payload["per_contract"]
        assert per_contract["pm-001"]["low_liquidity"] is True

    def test_low_liquidity_tag_suppresses_at_10001_volume(self, session: Session) -> None:
        _add_contract(session, contract_id="pm-001")
        _add_history_row(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.55,
            delta_pp=0.0,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            volume_24h_usd=10_001.0,
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        delta_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_delta"]
        per_contract = delta_blocks[0].payload["per_contract"]
        assert per_contract["pm-001"]["low_liquidity"] is False


# ---------------------------------------------------------------------------
# Trailing 30-day history populates from distillation_contract_history
# ---------------------------------------------------------------------------


class TestPredictionMarketTrailingHistory:
    def test_history_excludes_rows_outside_30_day_window(self, session: Session) -> None:
        """The 30-day-window filter on ``distillation_contract_history`` is
        applied at load time; rows outside the window never reach the brief
        payload regardless of ALP-633's downstream trim cap."""
        _add_contract(session, contract_id="pm-001", category="monetary_policy")
        # 2 snapshots within 30 days — fewer than the trim cap so the in-window
        # set passes through untouched and the filter assertion is unambiguous.
        for day in range(1, 3):
            _add_history_row(
                session,
                contract_id="pm-001",
                snapshot_ts=f"2026-04-{20 + day:02d}T00:00:00Z",
                yes_probability=0.4 + 0.02 * day,
            )
        # Outside the 30d window — must not appear in the trailing tuple.
        _add_history_row(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-03-01T00:00:00Z",
            yes_probability=0.30,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-25T00:00:00Z",
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        delta_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_delta"]
        per_contract = delta_blocks[0].payload["per_contract"]
        history = per_contract["pm-001"]["trailing_history"]
        assert len(history) == 2
        assert history[0][0] == "2026-04-21T00:00:00Z"
        assert history[-1][0] == "2026-04-22T00:00:00Z"
        timestamps = [row[0] for row in history]
        assert timestamps == sorted(timestamps)
        assert "2026-03-01T00:00:00Z" not in timestamps

    def test_history_trimmed_to_last_three_when_window_holds_more(
        self, session: Session
    ) -> None:
        """ALP-633: when more than three in-window rows exist, the brief
        payload's ``trailing_history`` tuple keeps only the three most
        recent so the per-contract row stays compact."""
        _add_contract(session, contract_id="pm-001", category="monetary_policy")
        for day in range(1, 6):
            _add_history_row(
                session,
                contract_id="pm-001",
                snapshot_ts=f"2026-04-{20 + day:02d}T00:00:00Z",
                yes_probability=0.4 + 0.02 * day,
            )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-25T00:00:00Z",
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        delta_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_delta"]
        per_contract = delta_blocks[0].payload["per_contract"]
        history = per_contract["pm-001"]["trailing_history"]
        assert [row[0] for row in history] == [
            "2026-04-23T00:00:00Z",
            "2026-04-24T00:00:00Z",
            "2026-04-25T00:00:00Z",
        ]


# ---------------------------------------------------------------------------
# Cross-platform liquidity-weighted normalization
# ---------------------------------------------------------------------------


class TestPredictionMarketCrossPlatformNormalization:
    def test_liquidity_weighted_normalization_for_matched_pair(self, session: Session) -> None:
        # Same outcome on Polymarket and Kalshi — same description tokens, same category
        _add_contract(
            session,
            contract_id="pm-poly-fed-jan",
            platform="polymarket",
            description="FOMC January rate hold",
            category="monetary_policy",
        )
        _add_contract(
            session,
            contract_id="pm-kalshi-fed-jan",
            platform="kalshi",
            description="FOMC January rate hold",
            category="monetary_policy",
        )
        # Polymarket: 0.60 with $80k liquidity
        _add_history_row(
            session,
            contract_id="pm-poly-fed-jan",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.60,
            liquidity_usd=80_000.0,
        )
        _add_snapshot(
            session,
            contract_id="pm-poly-fed-jan",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.60,
            liquidity_usd=80_000.0,
            volume_24h_usd=200_000.0,
        )
        # Kalshi: 0.50 with $20k liquidity
        _add_history_row(
            session,
            contract_id="pm-kalshi-fed-jan",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.50,
            liquidity_usd=20_000.0,
        )
        _add_snapshot(
            session,
            contract_id="pm-kalshi-fed-jan",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.50,
            liquidity_usd=20_000.0,
            volume_24h_usd=50_000.0,
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-poly-fed-jan", "pm-kalshi-fed-jan"),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        normalized_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_normalized"]
        assert len(normalized_blocks) == 1
        block = normalized_blocks[0]
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience
        groups = block.payload["groups"]
        assert len(groups) == 1
        # Liquidity-weighted: (0.60 * 80k + 0.50 * 20k) / 100k = (48 + 10) / 100 = 0.58
        normalized = next(iter(groups.values()))
        assert normalized["normalized_yes_probability"] == pytest.approx(0.58)
        assert normalized["total_liquidity_usd"] == pytest.approx(100_000.0)

    def test_no_match_no_normalized_block(self, session: Session) -> None:
        _add_contract(
            session,
            contract_id="pm-001",
            description="Will the Fed hold rates",
            category="monetary_policy",
        )
        _add_contract(
            session,
            contract_id="pm-002",
            description="Will Bitcoin hit 100k by year end",
            category="crypto",
        )
        _add_history_row(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.60,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
        )
        _add_history_row(
            session,
            contract_id="pm-002",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.20,
        )
        _add_snapshot(
            session,
            contract_id="pm-002",
            snapshot_ts="2026-04-26T00:00:00Z",
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001", "pm-002"),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        normalized_blocks = [b for b in blocks if b.block_id == "qual.prediction_market_normalized"]
        assert len(normalized_blocks) == 0


# ---------------------------------------------------------------------------
# Universal-broadcast audience for prediction-market outputs
# ---------------------------------------------------------------------------


class TestPredictionMarketAudience:
    def test_delta_block_carries_universal_broadcast_audience(self, session: Session) -> None:
        _add_contract(session, contract_id="pm-001")
        _add_history_row(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
            yes_probability=0.55,
            delta_pp=0.0,
        )
        _add_snapshot(
            session,
            contract_id="pm-001",
            snapshot_ts="2026-04-26T00:00:00Z",
        )
        session.commit()

        blocks = compute_prediction_market_deltas(
            session,
            contract_scope=("pm-001",),
            as_of="2026-04-26T00:00:00Z",
            delta_pp_threshold=DELTA_PP_THRESHOLD,
            low_liquidity_volume_min_usd=LOW_LIQUIDITY_VOLUME_MIN_USD,
            prediction_market_history_days=HISTORY_DAYS,
        )
        assert all(OutputAudience.UNIVERSAL_BROADCAST in block.audience for block in blocks)
