"""PM-accuracy + modification-effectiveness metrics (ALP-887 / story 06e).

The PM-accuracy metric cores compute purely over a hand-built ``WindowDataset``
whose ``replays`` bundle carries loader-assembled ``ReplayObservation``s
(counterfactual replays do not exist in production until the ALP-129 engine
populates them). The four cores:

* PM rejection accuracy — over evaluated medium/high-confidence ``rejection``
  replays, the fraction where the rejection was correct (the un-rejected trade
  would not have been profitable),
* modification effectiveness — over ``modification_original_form`` replays, the
  fraction where the actual modified-form outcome beat the counterfactual
  original-form outcome,
* sizing-modification effectiveness — the same comparison restricted to
  sizing-down (``risk_reduction``) modifications,
* anti-pattern detector accuracy — over rejection replays tagged with the
  conditioned anti-pattern, the fraction whose counterfactual confirms the tag.

Every core excludes low-confidence and unevaluable replays from aggregation
while the bundle still carries them, and degrades to the insufficient-sample
result on an empty bundle.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.feedback_loop.dataset import ReplaysBundle, WindowDataset
from alphamind.feedback_loop.metrics import get_metric, list_metrics
from alphamind.feedback_loop.metrics.pm_accuracy import (
    METRIC_ANTI_PATTERN_DETECTOR_ACCURACY,
    METRIC_MODIFICATION_EFFECTIVENESS,
    METRIC_PM_REJECTION_ACCURACY,
    METRIC_SIZING_MODIFICATION_EFFECTIVENESS,
    METRICS,
)
from alphamind.feedback_loop.metrics.types import (
    UNCONDITIONED,
    Conditioning,
    ConditioningDimension,
)
from alphamind.state.tables.counterfactual_replays import (
    Confidence,
    ReplayKind,
    ReplayStatus,
)
from tests.feedback_loop.metrics._replay_fixtures import make_replay_observation

_WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 4, 1, tzinfo=UTC)


def _dataset(*observations: object) -> WindowDataset:
    return WindowDataset(
        start=_WINDOW_START,
        end=_WINDOW_END,
        agent_calls=(),
        pm_decision_log=(),
        validations=(),
        replays=ReplaysBundle(replays=observations),  # type: ignore[arg-type]
    )


def test_pm_rejection_accuracy_counts_correct_rejections() -> None:
    # Two rejected proposals: one would have lost (correct rejection), one would
    # have won (PM was wrong to reject). Accuracy = 1/2.
    dataset = _dataset(
        make_replay_observation(envelope_id="ENV-REC-1", counterfactual_pnl=-50.0),
        make_replay_observation(envelope_id="ENV-REC-2", counterfactual_pnl=80.0),
    )
    result = get_metric(METRIC_PM_REJECTION_ACCURACY).compute(dataset, UNCONDITIONED)  # type: ignore[union-attr]
    assert result.value == 0.5
    assert result.sample_size == 2


def test_pm_rejection_accuracy_excludes_low_confidence_and_unevaluable() -> None:
    # One eligible correct rejection; a low-confidence and an unevaluable replay
    # are loaded but must not enter the aggregate. Accuracy = 1/1, sample_size = 1.
    dataset = _dataset(
        make_replay_observation(envelope_id="ENV-REC-1", counterfactual_pnl=-50.0),
        make_replay_observation(
            envelope_id="ENV-REC-2",
            confidence=Confidence.LOW,
            counterfactual_pnl=90.0,
        ),
        make_replay_observation(
            envelope_id="ENV-REC-3",
            replay_status=ReplayStatus.UNEVALUABLE,
            confidence=None,
            counterfactual_pnl=None,
        ),
    )
    result = get_metric(METRIC_PM_REJECTION_ACCURACY).compute(dataset, UNCONDITIONED)  # type: ignore[union-attr]
    assert result.value == 1.0
    assert result.sample_size == 1


def test_pm_rejection_accuracy_excludes_unpriceable_counterfactual() -> None:
    # An evaluated, medium/high-confidence rejection whose un-rejected trade the
    # engine could not price (counterfactual_pnl is None) has no baseline to judge
    # the rejection — it is excluded from the denominator, not counted as a wrong
    # rejection. Only the one priced correct rejection counts: accuracy = 1/1,
    # sample_size = 1. Pre-fix this read 1/2 (the None replay was a denominator-only
    # "wrong" rejection, biasing the rate downward).
    dataset = _dataset(
        make_replay_observation(envelope_id="ENV-REC-1", counterfactual_pnl=-50.0),
        make_replay_observation(envelope_id="ENV-REC-2", counterfactual_pnl=None),
    )
    result = get_metric(METRIC_PM_REJECTION_ACCURACY).compute(dataset, UNCONDITIONED)  # type: ignore[union-attr]
    assert result.value == 1.0
    assert result.sample_size == 1


def test_modification_effectiveness_compares_modified_to_counterfactual() -> None:
    # Three PM-modified proposals. The modified-form trade outperforms the
    # counterfactual original-form when actual beats counterfactual. m1 actual 120
    # vs original 50 helped; m2 actual -30 vs original 40 hurt; m3 actual 200 vs
    # original -10 helped. Two of three helped, so effectiveness is two thirds.
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=50.0,
            actual_modified_pnl=120.0,
        ),
        make_replay_observation(
            envelope_id="ENV-REC-2",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=40.0,
            actual_modified_pnl=-30.0,
        ),
        make_replay_observation(
            envelope_id="ENV-REC-3",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=-10.0,
            actual_modified_pnl=200.0,
        ),
    )
    result = get_metric(METRIC_MODIFICATION_EFFECTIVENESS).compute(dataset, UNCONDITIONED)  # type: ignore[union-attr]
    assert result.value == 2 / 3
    assert result.sample_size == 3


def test_modification_effectiveness_excludes_unresolved_modified_form() -> None:
    # A modification replay whose actual modified-form trade has not yet resolved
    # (actual_modified_pnl is None) is loaded but cannot be scored — it is excluded
    # from the comparison. Only the one resolved pair counts.
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=50.0,
            actual_modified_pnl=120.0,
        ),
        make_replay_observation(
            envelope_id="ENV-REC-2",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=40.0,
            actual_modified_pnl=None,
        ),
    )
    result = get_metric(METRIC_MODIFICATION_EFFECTIVENESS).compute(dataset, UNCONDITIONED)  # type: ignore[union-attr]
    assert result.value == 1.0
    assert result.sample_size == 1


def test_modification_effectiveness_excludes_unpriceable_counterfactual() -> None:
    # A modification replay whose original form could not be priced
    # (counterfactual_pnl is None — the engine evaluated the replay but the entry
    # window expired unfilled) has no baseline to compare the modified form against.
    # It is loaded but excluded from scoring rather than scored against a fabricated
    # 0.0 baseline. Only the priced pair counts.
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=50.0,
            actual_modified_pnl=120.0,
        ),
        make_replay_observation(
            envelope_id="ENV-REC-2",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=None,
            actual_modified_pnl=80.0,
        ),
    )
    result = get_metric(METRIC_MODIFICATION_EFFECTIVENESS).compute(dataset, UNCONDITIONED)  # type: ignore[union-attr]
    assert result.value == 1.0
    assert result.sample_size == 1


def test_modification_effectiveness_ignores_rejection_replays() -> None:
    # A rejection replay must not enter the modification metric.
    dataset = _dataset(
        make_replay_observation(envelope_id="ENV-REC-1", counterfactual_pnl=-50.0),
    )
    result = get_metric(METRIC_MODIFICATION_EFFECTIVENESS).compute(dataset, UNCONDITIONED)  # type: ignore[union-attr]
    assert result.value is None
    assert result.sample_size == 0
    assert result.insufficient_sample is True


def test_sizing_modification_effectiveness_filters_to_sizing_subset() -> None:
    # Two modification replays; only the sizing-down one counts toward the sizing
    # metric. The sizing modification helped (actual +120 > original +50).
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=50.0,
            actual_modified_pnl=120.0,
            is_sizing_modification=True,
        ),
        make_replay_observation(
            envelope_id="ENV-REC-2",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=40.0,
            actual_modified_pnl=-30.0,
            is_sizing_modification=False,
        ),
    )
    result = get_metric(METRIC_SIZING_MODIFICATION_EFFECTIVENESS).compute(  # type: ignore[union-attr]
        dataset, UNCONDITIONED
    )
    assert result.value == 1.0
    assert result.sample_size == 1


def test_sizing_modification_effectiveness_excludes_unpriceable_counterfactual() -> None:
    # The sizing subset shares the _scorable_modification gate: a sizing-down
    # modification whose original form could not be priced (counterfactual_pnl is
    # None) has no baseline and is excluded from pm_sizing_modification_effectiveness
    # rather than scored against a fabricated 0.0. Only the priced sizing pair counts.
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=50.0,
            actual_modified_pnl=120.0,
            is_sizing_modification=True,
        ),
        make_replay_observation(
            envelope_id="ENV-REC-2",
            replay_kind=ReplayKind.MODIFICATION_ORIGINAL_FORM,
            counterfactual_pnl=None,
            actual_modified_pnl=80.0,
            is_sizing_modification=True,
        ),
    )
    result = get_metric(METRIC_SIZING_MODIFICATION_EFFECTIVENESS).compute(  # type: ignore[union-attr]
        dataset, UNCONDITIONED
    )
    assert result.value == 1.0
    assert result.sample_size == 1


def test_anti_pattern_detector_accuracy_conditioned_on_pattern() -> None:
    # Three rejection replays tagged with sunk_cost_persistence; the metric asks,
    # per pattern, whether the counterfactual confirms the tag was right (the
    # rejected trade would not have been profitable). Two confirm, one refutes.
    # Accuracy for sunk_cost_persistence = 2/3.
    tag = "sunk_cost_persistence"
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1", counterfactual_pnl=-50.0, anti_patterns=(tag,)
        ),
        make_replay_observation(
            envelope_id="ENV-REC-2", counterfactual_pnl=-10.0, anti_patterns=(tag,)
        ),
        make_replay_observation(
            envelope_id="ENV-REC-3", counterfactual_pnl=70.0, anti_patterns=(tag,)
        ),
        # A different pattern — must not enter the sunk_cost_persistence slice.
        make_replay_observation(
            envelope_id="ENV-REC-4",
            counterfactual_pnl=200.0,
            anti_patterns=("conviction_inflation",),
        ),
    )
    metric = get_metric(METRIC_ANTI_PATTERN_DETECTOR_ACCURACY)
    result = metric.compute(dataset, Conditioning(ConditioningDimension.ANTI_PATTERN, tag))  # type: ignore[union-attr]
    assert result.value == 2 / 3
    assert result.sample_size == 3


def test_anti_pattern_detector_accuracy_excludes_unpriceable_counterfactual() -> None:
    # An evaluated, eligible rejection tagged with the pattern but whose
    # counterfactual the engine could not price has no baseline to confirm the tag —
    # it is excluded from the per-pattern denominator, not counted as a missed
    # detection. Only the one priced correct tag counts: accuracy = 1/1,
    # sample_size = 1. Pre-fix this read 1/2.
    tag = "sunk_cost_persistence"
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1", counterfactual_pnl=-50.0, anti_patterns=(tag,)
        ),
        make_replay_observation(
            envelope_id="ENV-REC-2", counterfactual_pnl=None, anti_patterns=(tag,)
        ),
    )
    metric = get_metric(METRIC_ANTI_PATTERN_DETECTOR_ACCURACY)
    result = metric.compute(dataset, Conditioning(ConditioningDimension.ANTI_PATTERN, tag))  # type: ignore[union-attr]
    assert result.value == 1.0
    assert result.sample_size == 1


def test_anti_pattern_detector_accuracy_unconditioned_is_empty() -> None:
    # The metric is meaningful only per pattern; the unconditioned whole-window
    # slice has no pattern to score and returns the insufficient-sample result.
    dataset = _dataset(
        make_replay_observation(
            envelope_id="ENV-REC-1",
            counterfactual_pnl=-50.0,
            anti_patterns=("sunk_cost_persistence",),
        ),
    )
    result = get_metric(METRIC_ANTI_PATTERN_DETECTOR_ACCURACY).compute(  # type: ignore[union-attr]
        dataset, UNCONDITIONED
    )
    assert result.value is None
    assert result.sample_size == 0
    assert result.insufficient_sample is True


def test_anti_pattern_detector_accuracy_supports_anti_pattern_conditioning() -> None:
    metric = get_metric(METRIC_ANTI_PATTERN_DETECTOR_ACCURACY)
    assert metric is not None
    assert ConditioningDimension.ANTI_PATTERN in metric.supported_conditioning


def test_all_four_metrics_registered_and_discoverable() -> None:
    # The four PM-accuracy metrics self-register via METRICS and are discoverable
    # by their stable ids through the package registry.
    expected = {
        METRIC_PM_REJECTION_ACCURACY,
        METRIC_MODIFICATION_EFFECTIVENESS,
        METRIC_SIZING_MODIFICATION_EFFECTIVENESS,
        METRIC_ANTI_PATTERN_DETECTOR_ACCURACY,
    }
    assert {m.metric_id for m in METRICS} == expected
    registered = {m.metric_id for m in list_metrics()}
    for metric_id in expected:
        assert metric_id in registered
        assert get_metric(metric_id) is not None


def test_every_metric_degrades_on_empty_replays() -> None:
    # The graceful-degradation contract: with no replays loaded (partial pre-engine
    # data), every PM-accuracy metric returns the insufficient-sample result rather
    # than raising.
    dataset = _dataset()
    for metric in METRICS:
        result = metric.compute(dataset, UNCONDITIONED)
        assert result.value is None
        assert result.sample_size == 0
        assert result.insufficient_sample is True
