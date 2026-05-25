"""Pure-compute tests for qualitative — no SQLite, no Session.

ALP-487 propagated the compute/load split to qualitative. The defining
property is that each ``qualitative/*_compute.py`` is testable from
hand-built frozen inputs with no ORM or in-memory database. Tests in this
module construct inputs directly and assert on the pure return shape.

The session-bound integration tests at
``tests/distillation/external/qualitative_derived/`` cover the IO shells
end-to-end; these tests cover the pure-compute boundaries.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._repository import (
    ContractCurrentStateRow,
    ContractHistoryEntry,
    ContractMetadataRow,
    NewsLabelCountsRow,
    TickerBaselineRow,
)
from alphamind.distillation.output import AnomalyFlag, OutputAudience, format_block
from alphamind.distillation.qualitative.news_price_divergence_compute import (
    NewsPriceDivergenceInputs,
    compute_news_price_divergence_blocks,
)
from alphamind.distillation.qualitative.prediction_market_deltas_compute import (
    PredictionMarketDeltasInputs,
    compute_prediction_market_delta_blocks,
)
from alphamind.distillation.qualitative.sentiment_percentile_compute import (
    SentimentPercentileInputs,
    compute_sentiment_percentile_blocks,
)

_AS_OF = datetime(2026, 5, 15, 16, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# News-price divergence
# ---------------------------------------------------------------------------


def _divergence_inputs(
    *,
    label_counts: dict[str, NewsLabelCountsRow],
    price_changes: dict[str, float | None],
    sector_audience: dict[str, OutputAudience] | None = None,
) -> NewsPriceDivergenceInputs:
    audience_map = sector_audience or {
        ticker: OutputAudience.SECTOR_TECH_SEMIS for ticker in label_counts
    }
    return NewsPriceDivergenceInputs(
        ticker_scope=tuple(sorted(label_counts)),
        freshness_ts=_AS_OF,
        sector_audience_by_ticker=audience_map,
        label_counts_by_ticker=label_counts,
        price_change_by_ticker=price_changes,
    )


class TestNewsPriceDivergence:
    def test_dominant_negative_news_with_rising_price_emits_priced_in(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "AAPL": NewsLabelCountsRow(positive=1, negative=9, neutral=0, mixed=0),
            },
            price_changes={"AAPL": 1.50},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert len(blocks) == 1
        entry = blocks[0].payload["per_ticker"]["AAPL"]
        assert entry["direction"] == "priced_in"
        assert entry["dominant_label"] == "negative"
        assert blocks[0].calibration_state is CalibrationState.CALIBRATED

    def test_dominant_positive_news_with_falling_price_emits_hidden_problem(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "MSFT": NewsLabelCountsRow(positive=9, negative=1, neutral=0, mixed=0),
            },
            price_changes={"MSFT": -2.0},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert blocks[0].payload["per_ticker"]["MSFT"]["direction"] == "hidden_problem"

    def test_agreement_does_not_fire(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "NVDA": NewsLabelCountsRow(positive=9, negative=1, neutral=0, mixed=0),
            },
            price_changes={"NVDA": 2.0},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert blocks == []

    def test_threshold_boundary_at_59_percent_no_dominant_label(self) -> None:
        # 59 positive / 41 negative = 59% positive — at-or-below the 60%
        # cutoff so no dominant direction is assigned.
        inputs = _divergence_inputs(
            label_counts={
                "AMD": NewsLabelCountsRow(positive=59, negative=41, neutral=0, mixed=0),
            },
            price_changes={"AMD": -0.5},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert blocks == []

    def test_threshold_boundary_at_61_percent_dominant_label_set(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "INTC": NewsLabelCountsRow(positive=61, negative=39, neutral=0, mixed=0),
            },
            price_changes={"INTC": -0.5},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert blocks[0].payload["per_ticker"]["INTC"]["dominant_label"] == "positive"

    def test_thin_evidence_below_min_articles_emits_bootstrap_block(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "AAPL": NewsLabelCountsRow(positive=0, negative=3, neutral=0, mixed=0),
            },
            price_changes={"AAPL": 0.10},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert blocks[0].calibration_state is CalibrationState.ACCUMULATING
        reason = blocks[0].bootstrap_reason
        assert reason is not None
        assert "news_price_divergence_min_articles: 3 < 5" in reason

    def test_unrouted_ticker_is_skipped(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "AAPL": NewsLabelCountsRow(positive=1, negative=9, neutral=0, mixed=0),
                "ORPHAN": NewsLabelCountsRow(positive=1, negative=9, neutral=0, mixed=0),
            },
            price_changes={"AAPL": 1.0, "ORPHAN": 1.0},
            sector_audience={"AAPL": OutputAudience.SECTOR_TECH_SEMIS},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert list(blocks[0].payload["per_ticker"]) == ["AAPL"]

    def test_missing_price_change_skips_ticker(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "AAPL": NewsLabelCountsRow(positive=1, negative=9, neutral=0, mixed=0),
            },
            price_changes={"AAPL": None},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        assert blocks == []

    def test_anomaly_flag_carries_magnitude_per_ticker(self) -> None:
        inputs = _divergence_inputs(
            label_counts={
                "AAPL": NewsLabelCountsRow(positive=1, negative=9, neutral=0, mixed=0),
            },
            price_changes={"AAPL": 1.0},
        )
        blocks = compute_news_price_divergence_blocks(inputs, min_articles=5)
        flags = blocks[0].anomaly_flags
        assert len(flags) == 1
        assert isinstance(flags[0], AnomalyFlag)
        assert flags[0].name == "news_price_divergence"


# ---------------------------------------------------------------------------
# Sentiment percentile
# ---------------------------------------------------------------------------


def _baseline(mean: float, stdev: float, n: int = 30) -> TickerBaselineRow:
    return TickerBaselineRow(
        ticker="X",
        baseline_kind="sentiment",
        as_of="2026-05-15T16:00:00Z",
        mean=mean,
        stdev=stdev,
        n_observations=n,
        window_days=30,
        calibration_state="calibrated",
    )


class TestSentimentPercentile:
    def test_current_above_baseline_yields_high_percentile(self) -> None:
        inputs = SentimentPercentileInputs(
            ticker_scope=("AAPL",),
            freshness_ts=_AS_OF,
            sector_audience_by_ticker={"AAPL": OutputAudience.SECTOR_TECH_SEMIS},
            current_sentiment_by_ticker={"AAPL": (0.50, 4)},
            baseline_by_ticker={"AAPL": _baseline(mean=0.0, stdev=0.25, n=30)},
            universe_pooled_sentiment=None,
        )
        blocks = compute_sentiment_percentile_blocks(inputs, sentiment_min_observations=30)
        entry = blocks[0].payload["per_ticker"]["AAPL"]
        assert entry["percentile"] > 90.0
        assert entry["calibration_state"] == CalibrationState.CALIBRATED.value
        assert blocks[0].calibration_state is CalibrationState.CALIBRATED

    def test_bootstrap_falls_back_to_universe_pool(self) -> None:
        # Per-ticker observations below threshold — falls back to universe pool.
        inputs = SentimentPercentileInputs(
            ticker_scope=("MSFT",),
            freshness_ts=_AS_OF,
            sector_audience_by_ticker={"MSFT": OutputAudience.SECTOR_TECH_SEMIS},
            current_sentiment_by_ticker={"MSFT": (0.20, 3)},
            baseline_by_ticker={"MSFT": _baseline(mean=0.0, stdev=0.30, n=5)},
            universe_pooled_sentiment=(0.05, 0.40),
        )
        blocks = compute_sentiment_percentile_blocks(inputs, sentiment_min_observations=30)
        entry = blocks[0].payload["per_ticker"]["MSFT"]
        assert entry["calibration_state"] == CalibrationState.ACCUMULATING.value
        assert entry["baseline_mean"] == pytest.approx(0.05)
        assert entry["baseline_stdev"] == pytest.approx(0.40)
        assert blocks[0].calibration_state is CalibrationState.ACCUMULATING

    def test_unavailable_when_pool_and_per_ticker_both_empty(self) -> None:
        inputs = SentimentPercentileInputs(
            ticker_scope=("NVDA",),
            freshness_ts=_AS_OF,
            sector_audience_by_ticker={"NVDA": OutputAudience.SECTOR_TECH_SEMIS},
            current_sentiment_by_ticker={"NVDA": (0.10, 2)},
            baseline_by_ticker={"NVDA": None},
            universe_pooled_sentiment=None,
        )
        blocks = compute_sentiment_percentile_blocks(inputs, sentiment_min_observations=30)
        # UNAVAILABLE — ticker omitted, no block emitted for the audience.
        assert blocks == []

    def test_missing_current_reading_skips_ticker(self) -> None:
        inputs = SentimentPercentileInputs(
            ticker_scope=("AAPL",),
            freshness_ts=_AS_OF,
            sector_audience_by_ticker={"AAPL": OutputAudience.SECTOR_TECH_SEMIS},
            current_sentiment_by_ticker={"AAPL": None},
            baseline_by_ticker={"AAPL": _baseline(0.0, 0.25)},
            universe_pooled_sentiment=None,
        )
        blocks = compute_sentiment_percentile_blocks(inputs, sentiment_min_observations=30)
        assert blocks == []

    def test_zero_stdev_collapses_percentile_to_50(self) -> None:
        inputs = SentimentPercentileInputs(
            ticker_scope=("ORCL",),
            freshness_ts=_AS_OF,
            sector_audience_by_ticker={"ORCL": OutputAudience.SECTOR_TECH_SEMIS},
            current_sentiment_by_ticker={"ORCL": (0.20, 4)},
            baseline_by_ticker={"ORCL": _baseline(mean=0.0, stdev=0.0, n=30)},
            universe_pooled_sentiment=None,
        )
        blocks = compute_sentiment_percentile_blocks(inputs, sentiment_min_observations=30)
        entry = blocks[0].payload["per_ticker"]["ORCL"]
        assert entry["percentile"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Prediction-market deltas
# ---------------------------------------------------------------------------


def _pm_inputs(
    *,
    current: dict[str, ContractCurrentStateRow | None],
    history: dict[str, tuple[ContractHistoryEntry, ...]] | None = None,
    metadata: dict[str, ContractMetadataRow | None] | None = None,
    volume_liquidity: dict[str, tuple[float, float]] | None = None,
) -> PredictionMarketDeltasInputs:
    return PredictionMarketDeltasInputs(
        contract_scope=tuple(sorted(current)),
        freshness_ts=_AS_OF,
        current_state_by_contract=current,
        history_by_contract=history or {cid: () for cid in current},
        metadata_by_contract=metadata or {cid: None for cid in current},
        volume_liquidity_by_contract=volume_liquidity or {cid: (0.0, 0.0) for cid in current},
    )


class TestPredictionMarketDeltas:
    def test_emits_one_delta_block_per_contract(self) -> None:
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.55,
                    delta_pp_since_prior=4.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket", description="FOMC rate hold", category="monetary_policy"
                ),
            },
            volume_liquidity={"POLY-1": (50_000.0, 100_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert len(blocks) == 1
        assert blocks[0].block_id == "qual.prediction_market_delta"
        entry = blocks[0].payload["per_contract"]["POLY-1"]
        assert entry["yes_probability"] == pytest.approx(0.55)
        assert entry["delta_pp_since_prior"] == pytest.approx(4.0)
        assert entry["delta_anomaly"] is False
        assert entry["low_liquidity"] is False

    def test_delta_anomaly_above_threshold_fires_flag(self) -> None:
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.60,
                    delta_pp_since_prior=8.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        delta_block = blocks[0]
        assert delta_block.payload["per_contract"]["POLY-1"]["delta_anomaly"] is True
        assert len(delta_block.anomaly_flags) == 1
        assert delta_block.anomaly_flags[0].name == "prediction_market_delta"
        assert delta_block.anomaly_flags[0].magnitude == pytest.approx(8.0)

    def test_low_liquidity_tag_fires_below_volume_threshold(self) -> None:
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.30,
                    delta_pp_since_prior=1.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="FOMC rate hold",
                    category="monetary_policy",
                ),
            },
            volume_liquidity={"POLY-1": (5_000.0, 50_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert blocks[0].payload["per_contract"]["POLY-1"]["low_liquidity"] is True

    def test_missing_current_state_omits_contract(self) -> None:
        inputs = _pm_inputs(current={"POLY-1": None})
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert blocks == []

    def test_cross_platform_match_emits_normalized_block(self) -> None:
        # Two contracts with the same category and same tokenized description.
        # Expected to group into one normalized block.
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.50,
                    delta_pp_since_prior=1.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
                "KAL-1": ContractCurrentStateRow(
                    yes_probability=0.70,
                    delta_pp_since_prior=2.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket", description="FOMC rate hold", category="monetary_policy"
                ),
                "KAL-1": ContractMetadataRow(
                    platform="kalshi", description="FOMC rate hold", category="monetary_policy"
                ),
            },
            volume_liquidity={"POLY-1": (50_000.0, 100_000.0), "KAL-1": (60_000.0, 300_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert {block.block_id for block in blocks} == {
            "qual.prediction_market_delta",
            "qual.prediction_market_normalized",
        }
        normalized_block = next(
            b for b in blocks if b.block_id == "qual.prediction_market_normalized"
        )
        groups = normalized_block.payload["groups"]
        # liquidity-weighted: (0.50*100k + 0.70*300k) / 400k = 0.65
        (group,) = groups.values()
        assert group["normalized_yes_probability"] == pytest.approx(0.65)

    def test_zero_liquidity_skips_normalization(self) -> None:
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.50,
                    delta_pp_since_prior=1.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
                "KAL-1": ContractCurrentStateRow(
                    yes_probability=0.70,
                    delta_pp_since_prior=2.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket", description="FOMC rate hold", category="monetary_policy"
                ),
                "KAL-1": ContractMetadataRow(
                    platform="kalshi", description="FOMC rate hold", category="monetary_policy"
                ),
            },
            volume_liquidity={"POLY-1": (50_000.0, 0.0), "KAL-1": (60_000.0, 0.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert {block.block_id for block in blocks} == {"qual.prediction_market_delta"}

    def test_empty_scope_emits_no_blocks(self) -> None:
        inputs = _pm_inputs(current={})
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert blocks == []

    def test_past_dated_question_flagged_but_kept(self) -> None:
        """A question text referencing a date before ``as_of`` carries
        ``is_question_past_dated=True`` in the per-contract payload (ALP-578)
        — the synthesizer's downstream consumers see the contract tagged
        rather than excluded. Uses a macro-relevance category so the
        contract survives ALP-633 brief curation despite the flat delta."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.0005,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T00:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will FOMC cut rates by May 6?",
                    category="monetary_policy",
                    resolution_date=date(2026, 5, 31),
                ),
            },
            volume_liquidity={"POLY-1": (100.0, 200.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        entry = blocks[0].payload["per_contract"]["POLY-1"]
        assert entry["is_question_past_dated"] is True

    def test_future_dated_question_not_past_dated(self) -> None:
        """A question with a future-dated reference is not flagged."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.6,
                    delta_pp_since_prior=2.0,
                    snapshot_ts="2026-05-15T00:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will the FOMC cut rates by December 15?",
                    category="monetary_policy",
                    resolution_date=date(2026, 12, 31),
                ),
            },
            volume_liquidity={"POLY-1": (200_000.0, 300_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        entry = blocks[0].payload["per_contract"]["POLY-1"]
        assert entry["is_question_past_dated"] is False

    def test_low_volume_flat_history_flagged_stale_low_signal(self) -> None:
        """Low liquidity AND every trailing snapshot at the same yes_probability
        → ``is_stale_low_signal=True`` in the per-contract payload (ALP-578).
        Mirrors the QR loader's likely-resolved heuristic."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.0005,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T00:00:00Z",
                ),
            },
            history={
                "POLY-1": (
                    ContractHistoryEntry(
                        snapshot_ts="2026-05-10T00:00:00Z", yes_probability=0.0005
                    ),
                    ContractHistoryEntry(
                        snapshot_ts="2026-05-15T00:00:00Z", yes_probability=0.0005
                    ),
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="FOMC rate hold",
                    category="monetary_policy",
                ),
            },
            volume_liquidity={"POLY-1": (10.0, 200.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        entry = blocks[0].payload["per_contract"]["POLY-1"]
        assert entry["is_stale_low_signal"] is True

    def test_high_volume_flat_history_not_stale_low_signal(self) -> None:
        """Flat history but high liquidity → not stale-low-signal."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.7,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T00:00:00Z",
                ),
            },
            history={
                "POLY-1": (
                    ContractHistoryEntry(snapshot_ts="2026-05-10T00:00:00Z", yes_probability=0.7),
                    ContractHistoryEntry(snapshot_ts="2026-05-15T00:00:00Z", yes_probability=0.7),
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="FOMC rate hold",
                    category="monetary_policy",
                ),
            },
            volume_liquidity={"POLY-1": (500_000.0, 100_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        entry = blocks[0].payload["per_contract"]["POLY-1"]
        assert entry["is_stale_low_signal"] is False

    def test_low_volume_moving_history_not_stale_low_signal(self) -> None:
        """Low liquidity but at least one trailing snapshot differs in
        yes_probability → not stale-low-signal."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.2,
                    delta_pp_since_prior=10.0,
                    snapshot_ts="2026-05-15T00:00:00Z",
                ),
            },
            history={
                "POLY-1": (
                    ContractHistoryEntry(snapshot_ts="2026-05-10T00:00:00Z", yes_probability=0.1),
                    ContractHistoryEntry(snapshot_ts="2026-05-15T00:00:00Z", yes_probability=0.2),
                ),
            },
            volume_liquidity={"POLY-1": (100.0, 200.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        entry = blocks[0].payload["per_contract"]["POLY-1"]
        assert entry["is_stale_low_signal"] is False

    def test_rendered_block_carries_staleness_flags(self) -> None:
        """The rendered ``qual.prediction_market_delta`` block — what the
        synthesizer's UNIVERSAL CONTEXT consumes — surfaces both staleness
        flags per contract (ALP-578 acceptance criterion)."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.0005,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T00:00:00Z",
                ),
            },
            history={
                "POLY-1": (
                    ContractHistoryEntry(
                        snapshot_ts="2026-05-15T00:00:00Z", yes_probability=0.0005
                    ),
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will FOMC cut rates by May 6?",
                    category="monetary_policy",
                    resolution_date=date(2026, 5, 31),
                ),
            },
            volume_liquidity={"POLY-1": (10.0, 200.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        rendered = format_block(blocks[0])
        assert "is_question_past_dated: True" in rendered
        assert "is_stale_low_signal: True" in rendered
        assert "low_liquidity: True" in rendered

    def test_anomalous_contract_emitted_regardless_of_category(self) -> None:
        """ALP-633 (a): a delta-anomalous contract is emitted into the brief
        payload even when its category is outside the macro-relevance
        allowlist — the anomaly itself is the signal."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.20,
                    delta_pp_since_prior=12.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will Sarah Huckabee Sanders win the 2028 nomination?",
                    category="election",
                ),
            },
            volume_liquidity={"POLY-1": (50_000.0, 100_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert len(blocks) == 1
        assert "POLY-1" in blocks[0].payload["per_contract"]
        assert blocks[0].payload["per_contract"]["POLY-1"]["category"] == "election"

    def test_non_anomalous_monetary_policy_contract_emitted(self) -> None:
        """ALP-633 (b): a non-anomalous ``monetary_policy`` contract is
        emitted because the level itself is tradeable (qualitative.md §3a —
        cross-referenced against quant 6b fed funds futures)."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.65,
                    delta_pp_since_prior=1.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="FOMC rate hold in June",
                    category="monetary_policy",
                ),
            },
            volume_liquidity={"POLY-1": (200_000.0, 500_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert len(blocks) == 1
        assert "POLY-1" in blocks[0].payload["per_contract"]

    def test_non_anomalous_opec_contract_emitted(self) -> None:
        """ALP-633 (b): a non-anomalous ``opec`` contract is emitted
        because OPEC production decisions are cross-referenced against
        quant 8a crude futures (qualitative.md §3c)."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.40,
                    delta_pp_since_prior=2.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="OPEC+ holds quotas at next meeting",
                    category="opec",
                ),
            },
            volume_liquidity={"POLY-1": (50_000.0, 80_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert len(blocks) == 1
        assert "POLY-1" in blocks[0].payload["per_contract"]

    def test_non_anomalous_election_longshot_suppressed(self) -> None:
        """ALP-633 (c): a non-anomalous ``election`` longshot is curated
        out of the brief — the level carries no tradeable information and
        flooded the synthesizer's input pre-ALP-633."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.0075,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will Ted Cruz win the 2028 Republican nomination?",
                    category="election",
                ),
            },
            volume_liquidity={"POLY-1": (36_790.0, 1_633_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert blocks == []

    def test_anomaly_flags_enumerate_pre_curation_universe(self) -> None:
        """ALP-633 (d): ``anomaly_flags`` reports every delta-anomalous
        contract independent of brief curation. A mixed-universe with one
        anomalous election + one non-anomalous election lands the
        anomalous row in both ``per_contract`` and ``anomaly_flags`` while
        the non-anomalous sibling is suppressed everywhere."""
        inputs = _pm_inputs(
            current={
                "POLY-ANOM": ContractCurrentStateRow(
                    yes_probability=0.30,
                    delta_pp_since_prior=15.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
                "POLY-FLAT": ContractCurrentStateRow(
                    yes_probability=0.005,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-ANOM": ContractMetadataRow(
                    platform="polymarket",
                    description="Will Hunter Biden win the 2028 nomination?",
                    category="election",
                ),
                "POLY-FLAT": ContractMetadataRow(
                    platform="polymarket",
                    description="Will Sarah Huckabee Sanders win the 2028 nomination?",
                    category="election",
                ),
            },
            volume_liquidity={
                "POLY-ANOM": (50_000.0, 100_000.0),
                "POLY-FLAT": (36_790.0, 1_633_000.0),
            },
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        delta_block = next(b for b in blocks if b.block_id == "qual.prediction_market_delta")
        assert set(delta_block.payload["per_contract"]) == {"POLY-ANOM"}
        assert [flag.name for flag in delta_block.anomaly_flags] == ["prediction_market_delta"]
        assert delta_block.anomaly_flags[0].magnitude == pytest.approx(15.0)

    def test_normalized_block_suppressed_when_no_constituent_passes_curation(self) -> None:
        """ALP-633: a cross-platform group of non-anomalous longshots in a
        non-allowlisted category produces no normalized block — otherwise
        the sibling block would re-introduce the noise the delta block
        just dropped."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.005,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
                "KAL-1": ContractCurrentStateRow(
                    yes_probability=0.006,
                    delta_pp_since_prior=0.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will Ted Cruz win 2028 nomination",
                    category="election",
                ),
                "KAL-1": ContractMetadataRow(
                    platform="kalshi",
                    description="Will Ted Cruz win 2028 nomination",
                    category="election",
                ),
            },
            volume_liquidity={
                "POLY-1": (40_000.0, 100_000.0),
                "KAL-1": (35_000.0, 100_000.0),
            },
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert blocks == []

    def test_normalized_block_emitted_when_any_constituent_passes_curation(self) -> None:
        """An anomalous constituent in a cross-platform group keeps the
        normalized block emission — the group is signal-bearing even if
        the other constituent is below threshold."""
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.55,
                    delta_pp_since_prior=12.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
                "KAL-1": ContractCurrentStateRow(
                    yes_probability=0.50,
                    delta_pp_since_prior=2.0,
                    snapshot_ts="2026-05-15T16:00:00Z",
                ),
            },
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will Ted Cruz win 2028 nomination",
                    category="election",
                ),
                "KAL-1": ContractMetadataRow(
                    platform="kalshi",
                    description="Will Ted Cruz win 2028 nomination",
                    category="election",
                ),
            },
            volume_liquidity={
                "POLY-1": (50_000.0, 100_000.0),
                "KAL-1": (60_000.0, 200_000.0),
            },
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        assert {block.block_id for block in blocks} == {
            "qual.prediction_market_delta",
            "qual.prediction_market_normalized",
        }

    def test_trailing_history_trimmed_to_three_most_recent_entries(self) -> None:
        """ALP-633: the emitted ``trailing_history`` carries at most the
        last three entries. The full series dominated the brief's byte
        budget at one contract per row pre-fix."""
        full_history = tuple(
            ContractHistoryEntry(
                snapshot_ts=f"2026-05-{day:02d}T00:00:00Z", yes_probability=0.50 + 0.01 * day
            )
            for day in range(1, 11)
        )
        inputs = _pm_inputs(
            current={
                "POLY-1": ContractCurrentStateRow(
                    yes_probability=0.60,
                    delta_pp_since_prior=10.0,
                    snapshot_ts="2026-05-10T00:00:00Z",
                ),
            },
            history={"POLY-1": full_history},
            metadata={
                "POLY-1": ContractMetadataRow(
                    platform="polymarket",
                    description="Will FOMC cut rates by June 15?",
                    category="monetary_policy",
                ),
            },
            volume_liquidity={"POLY-1": (100_000.0, 200_000.0)},
        )
        blocks = compute_prediction_market_delta_blocks(
            inputs, delta_pp_threshold=5.0, low_liquidity_volume_min_usd=10_000.0
        )
        history_tuple = blocks[0].payload["per_contract"]["POLY-1"]["trailing_history"]
        assert len(history_tuple) == 3
        assert [ts for ts, _ in history_tuple] == [
            "2026-05-08T00:00:00Z",
            "2026-05-09T00:00:00Z",
            "2026-05-10T00:00:00Z",
        ]
