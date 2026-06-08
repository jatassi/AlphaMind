"""Outcome + calibration metrics (ALP-885 / story 06c).

The outcome-tier metric cores compute purely over a hand-built ``WindowDataset``
(resolved theses do not exist in production until 04e), with:

* conviction / status calibration as rates with an 80% posterior band,
* the outcome surface (win rate, profit factor, drawdown, resolution
  distribution, P/L per resolved thesis, P/L per token),
* the insufficient-sample flag driven by the bundle's sample-size threshold,
* a conditioning slice (regime) that recomputes an outcome metric over a subset,
* every metric registered with a stable ``MetricId``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from alphamind.feedback_loop.dataset import OutcomesBundle, WindowDataset
from alphamind.feedback_loop.metrics import get_metric
from alphamind.feedback_loop.metrics.outcomes import (
    METRIC_CONVICTION_CALIBRATION,
    METRIC_DRAWDOWN,
    METRIC_PL_PER_RESOLVED_THESIS,
    METRIC_PL_PER_TOKEN,
    METRIC_PROFIT_FACTOR,
    METRIC_RESOLUTION_DISTRIBUTION,
    METRIC_STATUS_CALIBRATION,
    METRIC_WEEKLY_PL,
    METRIC_WIN_RATE,
    METRICS,
)
from alphamind.feedback_loop.metrics.types import (
    POSTERIOR_BAND_WIDTH,
    UNCONDITIONED,
    Conditioning,
    ConditioningDimension,
)
from tests.feedback_loop.metrics._outcome_fixtures import make_thesis_outcome

_WINDOW_START = datetime(2026, 4, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)


def _dataset(
    *outcomes: object,
    monthly_threshold: int = 30,
    quarterly_threshold: int = 60,
    agent_calls: tuple[object, ...] = (),
) -> WindowDataset:
    return WindowDataset(
        start=_WINDOW_START,
        end=_WINDOW_END,
        agent_calls=agent_calls,  # type: ignore[arg-type]
        pm_decision_log=(),
        validations=(),
        outcomes=OutcomesBundle(
            theses=outcomes,  # type: ignore[arg-type]
            min_resolved_theses_monthly=monthly_threshold,
            min_resolved_theses_quarterly=quarterly_threshold,
        ),
    )


def _agent_call(input_tokens: int, output_tokens: int) -> object:
    from alphamind.state.tables.agent_calls import AgentCallRecord

    return AgentCallRecord(
        agent_call_id="c",
        invocation_id="i",
        agent_name="analyst",
        attempt_number=1,
        model_id="claude-opus-4-8",
        prompt_path="p",
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json="{}",
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        wall_clock_ms=1,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=None,
    )


class TestWinRate:
    def test_win_rate_is_fraction_profitable(self) -> None:
        metric = get_metric(METRIC_WIN_RATE)
        assert metric is not None
        # 3 wins, 1 loss -> 0.75
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=50.0),
            make_thesis_outcome(thesis_id="t3", resolution_pnl_usd=5.0),
            make_thesis_outcome(thesis_id="t4", resolution_pnl_usd=-20.0),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == 0.75
        assert result.sample_size == 4

    def test_win_rate_carries_posterior_band(self) -> None:
        metric = get_metric(METRIC_WIN_RATE)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=-20.0),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value is not None
        assert result.posterior_band is not None
        assert result.posterior_band.lower < result.value < result.posterior_band.upper

    def test_empty_sample_yields_none_value_and_no_band(self) -> None:
        metric = get_metric(METRIC_WIN_RATE)
        assert metric is not None
        result = metric.compute(_dataset(), UNCONDITIONED)
        assert result.value is None
        assert result.posterior_band is None
        assert result.sample_size == 0


class TestProfitFactor:
    def test_profit_factor_is_wins_over_losses(self) -> None:
        metric = get_metric(METRIC_PROFIT_FACTOR)
        assert metric is not None
        # wins = 150, losses = 50 -> 3.0
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=50.0),
            make_thesis_outcome(thesis_id="t3", resolution_pnl_usd=-50.0),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == 3.0

    def test_profit_factor_all_wins_is_inf(self) -> None:
        # An all-winning slice (wins > 0, no losses) has a mathematically-infinite
        # profit factor — distinct from the undefined / no-data None (ALP-912 F).
        metric = get_metric(METRIC_PROFIT_FACTOR)
        assert metric is not None
        dataset = _dataset(make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0))
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == math.inf
        assert result.sample_size == 1

    def test_profit_factor_all_break_even_is_none(self) -> None:
        # A non-empty slice with neither wins nor losses (all 0.0) is the genuinely
        # undefined 0/0 case — value stays None, not inf.
        metric = get_metric(METRIC_PROFIT_FACTOR)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=0.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=0.0),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value is None
        assert result.sample_size == 2

    def test_profit_factor_empty_slice_is_none(self) -> None:
        # The empty slice routes through the shared empty builder: value None,
        # sample_size 0 — distinct from the all-wins inf above.
        metric = get_metric(METRIC_PROFIT_FACTOR)
        assert metric is not None
        result = metric.compute(_dataset(), UNCONDITIONED)
        assert result.value is None
        assert result.sample_size == 0


class TestDrawdown:
    def test_drawdown_is_max_peak_to_trough(self) -> None:
        metric = get_metric(METRIC_DRAWDOWN)
        assert metric is not None
        # cumulative: +100, +60 (-40), +160 -> max drawdown = 40
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=-40.0),
            make_thesis_outcome(thesis_id="t3", resolution_pnl_usd=100.0),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == 40.0


class TestResolutionDistribution:
    def test_distribution_fraction_validated(self) -> None:
        from alphamind.portfolio_state.records.theses import ThesisResolutionCategory

        metric = get_metric(METRIC_RESOLUTION_DISTRIBUTION)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(
                thesis_id="t1", resolution_category=ThesisResolutionCategory.VALIDATED
            ),
            make_thesis_outcome(
                thesis_id="t2", resolution_category=ThesisResolutionCategory.VALIDATED
            ),
            make_thesis_outcome(
                thesis_id="t3",
                resolution_category=ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT,
            ),
        )
        # Unconditioned: fraction VALIDATED = 2/3
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value is not None
        assert abs(result.value - (2.0 / 3.0)) < 1e-9


class TestPlPerResolvedThesis:
    def test_average_pl(self) -> None:
        metric = get_metric(METRIC_PL_PER_RESOLVED_THESIS)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=-40.0),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == 30.0
        assert result.posterior_band is not None


class TestPlPerToken:
    def test_pl_per_token(self) -> None:
        metric = get_metric(METRIC_PL_PER_TOKEN)
        assert metric is not None
        # total P/L = 200; tokens = (1000 in + 500 out) + (500 in + 0 out) = 2000
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=120.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=80.0),
            agent_calls=(_agent_call(1000, 500), _agent_call(500, 0)),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == 0.1  # 200 / 2000

    def test_pl_per_token_none_when_no_tokens(self) -> None:
        metric = get_metric(METRIC_PL_PER_TOKEN)
        assert metric is not None
        dataset = _dataset(make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=120.0))
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value is None


class TestConvictionCalibration:
    def test_per_conviction_win_rate_via_slice(self) -> None:
        metric = get_metric(METRIC_CONVICTION_CALIBRATION)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", conviction="4", resolution_pnl_usd=100.0),
            make_thesis_outcome(thesis_id="t2", conviction="4", resolution_pnl_usd=50.0),
            make_thesis_outcome(thesis_id="t3", conviction="2", resolution_pnl_usd=-10.0),
        )
        high = metric.compute(
            dataset, Conditioning(dimension=ConditioningDimension.CONVICTION, value="4")
        )
        assert high.value == 1.0
        assert high.sample_size == 2
        assert high.posterior_band is not None
        low = metric.compute(
            dataset, Conditioning(dimension=ConditioningDimension.CONVICTION, value="2")
        )
        assert low.value == 0.0
        assert low.sample_size == 1


class TestStatusCalibration:
    def test_per_status_adverse_rate_via_slice(self) -> None:
        metric = get_metric(METRIC_STATUS_CALIBRATION)
        assert metric is not None
        # at-risk: 1 adverse of 2 -> 0.5 ; on-track: 0 adverse of 1 -> 0.0
        dataset = _dataset(
            make_thesis_outcome(
                thesis_id="t1", strategist_status="at-risk", resolution_pnl_usd=-10.0
            ),
            make_thesis_outcome(
                thesis_id="t2", strategist_status="at-risk", resolution_pnl_usd=20.0
            ),
            make_thesis_outcome(
                thesis_id="t3", strategist_status="on-track", resolution_pnl_usd=30.0
            ),
        )
        at_risk = metric.compute(
            dataset,
            Conditioning(dimension=ConditioningDimension.STRATEGIST_STATUS, value="at-risk"),
        )
        assert at_risk.value == 0.5
        assert at_risk.posterior_band is not None
        on_track = metric.compute(
            dataset,
            Conditioning(dimension=ConditioningDimension.STRATEGIST_STATUS, value="on-track"),
        )
        assert on_track.value == 0.0


class TestConditioningSlice:
    def test_win_rate_recomputes_under_regime_slice(self) -> None:
        metric = get_metric(METRIC_WIN_RATE)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", regime="elevated", resolution_pnl_usd=100.0),
            make_thesis_outcome(thesis_id="t2", regime="elevated", resolution_pnl_usd=-10.0),
            make_thesis_outcome(thesis_id="t3", regime="normal", resolution_pnl_usd=50.0),
        )
        elevated = metric.compute(
            dataset, Conditioning(dimension=ConditioningDimension.REGIME, value="elevated")
        )
        assert elevated.value == 0.5
        assert elevated.sample_size == 2
        whole = metric.compute(dataset, UNCONDITIONED)
        assert whole.sample_size == 3


class TestInsufficientSample:
    def test_flag_set_below_threshold(self) -> None:
        metric = get_metric(METRIC_WIN_RATE)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0),
            monthly_threshold=5,
            quarterly_threshold=5,
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.insufficient_sample is True

    def test_flag_clear_at_threshold(self) -> None:
        metric = get_metric(METRIC_WIN_RATE)
        assert metric is not None
        outcomes = tuple(
            make_thesis_outcome(thesis_id=f"t{i}", resolution_pnl_usd=10.0) for i in range(5)
        )
        dataset = _dataset(*outcomes, monthly_threshold=5, quarterly_threshold=5)
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.insufficient_sample is False

    def test_calibration_uses_quarterly_threshold(self) -> None:
        metric = get_metric(METRIC_CONVICTION_CALIBRATION)
        assert metric is not None
        # 4 resolved at conviction 4; monthly threshold low, quarterly high ->
        # calibration (quarterly-tier) must read insufficient.
        outcomes = tuple(
            make_thesis_outcome(thesis_id=f"t{i}", conviction="4", resolution_pnl_usd=10.0)
            for i in range(4)
        )
        dataset = _dataset(*outcomes, monthly_threshold=1, quarterly_threshold=10)
        result = metric.compute(
            dataset, Conditioning(dimension=ConditioningDimension.CONVICTION, value="4")
        )
        assert result.insufficient_sample is True


class TestWeeklyPl:
    def test_registration_outcome_weekly_window(self) -> None:
        from alphamind.feedback_loop.metrics.types import Window

        metric = get_metric(METRIC_WEEKLY_PL)
        assert metric is not None
        assert metric.po_type == "outcome"
        assert metric.default_window is Window.WEEKLY

    def test_sum_mixed_sign_pnl(self) -> None:
        metric = get_metric(METRIC_WEEKLY_PL)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=200.0),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=-50.0),
            make_thesis_outcome(thesis_id="t3", resolution_pnl_usd=30.0),
        )
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value == 180.0
        assert result.sample_size == 3
        assert result.posterior_band is None

    def test_empty_slice_yields_none_value_zero_sample(self) -> None:
        metric = get_metric(METRIC_WEEKLY_PL)
        assert metric is not None
        result = metric.compute(_dataset(), UNCONDITIONED)
        assert result.value is None
        assert result.sample_size == 0
        assert result.posterior_band is None

    def test_regime_conditioning_filters_correctly(self) -> None:
        metric = get_metric(METRIC_WEEKLY_PL)
        assert metric is not None
        dataset = _dataset(
            make_thesis_outcome(thesis_id="t1", resolution_pnl_usd=100.0, regime="elevated"),
            make_thesis_outcome(thesis_id="t2", resolution_pnl_usd=50.0, regime="elevated"),
            make_thesis_outcome(thesis_id="t3", resolution_pnl_usd=-200.0, regime="calm"),
        )
        result = metric.compute(
            dataset, Conditioning(dimension=ConditioningDimension.REGIME, value="elevated")
        )
        assert result.value == 150.0
        assert result.sample_size == 2
        assert result.posterior_band is None


class TestRegistration:
    def test_all_metrics_discoverable(self) -> None:
        for metric in METRICS:
            assert get_metric(metric.metric_id) is metric

    def test_metric_ids_stable_and_outcome_tier(self) -> None:
        ids = {m.metric_id for m in METRICS}
        expected = {
            METRIC_WIN_RATE,
            METRIC_PROFIT_FACTOR,
            METRIC_DRAWDOWN,
            METRIC_RESOLUTION_DISTRIBUTION,
            METRIC_PL_PER_RESOLVED_THESIS,
            METRIC_PL_PER_TOKEN,
            METRIC_CONVICTION_CALIBRATION,
            METRIC_STATUS_CALIBRATION,
            METRIC_WEEKLY_PL,
        }
        assert expected <= ids
        for metric in METRICS:
            assert metric.po_type == "outcome"

    def test_posterior_band_width_definitional(self) -> None:
        # The 80% band is definitional in metrics/types.py, not config.
        assert POSTERIOR_BAND_WIDTH == 0.80
