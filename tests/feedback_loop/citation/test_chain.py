"""Citation-chain metric cores (ALP-886 story 06d).

Five pure ``Metric`` cores over a hand-built :class:`WindowDataset.refs` —
synthesizer citation rate per source, synthesizer recall, decision-layer
citation rate per source, signal survival rate per source, per-source validation
rate. No DB: every compute runs over a constructed ``RefsBundle`` fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.feedback_loop.citation.chain import (
    METRICS,
    metric_id_for,
)
from alphamind.feedback_loop.citation.parser import (
    CitationSource,
    ExtractedRef,
    RefChain,
)
from alphamind.feedback_loop.dataset import RefsBundle, WindowDataset
from alphamind.feedback_loop.metrics import get_metric, list_metrics
from alphamind.feedback_loop.metrics.types import UNCONDITIONED, MetricId
from alphamind.portfolio_state.records.theses import ThesisComponentOutcome

_START = datetime(2026, 5, 1, tzinfo=UTC)
_END = datetime(2026, 7, 1, tzinfo=UTC)


def _chain(
    ref_id: str,
    source: CitationSource,
    *,
    synth: bool = False,
    decision: bool = False,
    outcome: ThesisComponentOutcome | None = None,
) -> RefChain:
    return RefChain(
        ref=ExtractedRef(ref_id=ref_id, source=source),
        cited_in_synthesis=synth,
        cited_in_decision=decision,
        thesis_component_outcome=outcome,
    )


def _dataset(*chains: RefChain) -> WindowDataset:
    return WindowDataset(
        start=_START,
        end=_END,
        agent_calls=(),
        pm_decision_log=(),
        validations=(),
        refs=RefsBundle(citations=chains),
    )


def test_synthesizer_citation_rate_per_source() -> None:
    # Two SA-TECH refs, one cited in synthesis → rate 0.5.
    dataset = _dataset(
        _chain("SA-TECH-1", CitationSource.SA_TECH, synth=True),
        _chain("SA-TECH-2", CitationSource.SA_TECH, synth=False),
    )
    metric = get_metric(metric_id_for("synthesizer_citation_rate", CitationSource.SA_TECH))
    assert metric is not None
    result = metric.compute(dataset, UNCONDITIONED)
    assert result.value == 0.5
    assert result.sample_size == 2


def test_decision_layer_citation_rate_per_source() -> None:
    dataset = _dataset(
        _chain("QR-1", CitationSource.QR, decision=True),
        _chain("QR-2", CitationSource.QR, decision=True),
        _chain("QR-3", CitationSource.QR, decision=False),
        _chain("AR-1", CitationSource.AR, decision=False),
    )
    metric = get_metric(metric_id_for("decision_layer_citation_rate", CitationSource.QR))
    assert metric is not None
    result = metric.compute(dataset, UNCONDITIONED)
    assert result.value == 2 / 3
    assert result.sample_size == 3  # AR ref excluded — different source


def test_signal_survival_rate_per_source_counts_validated_over_all_from_source() -> None:
    # Denominator is *all* refs from the source, not just resolved ones.
    dataset = _dataset(
        _chain("SA-FIN-1", CitationSource.SA_FIN, outcome=ThesisComponentOutcome.VALIDATED),
        _chain("SA-FIN-2", CitationSource.SA_FIN, outcome=ThesisComponentOutcome.WRONG),
        _chain("SA-FIN-3", CitationSource.SA_FIN, outcome=None),  # never a component
    )
    metric = get_metric(metric_id_for("signal_survival_rate", CitationSource.SA_FIN))
    assert metric is not None
    result = metric.compute(dataset, UNCONDITIONED)
    assert result.value == 1 / 3
    assert result.sample_size == 3


def test_per_source_validation_rate_only_counts_resolved_components() -> None:
    # Denominator is refs that reached a *resolved* component, excluding the
    # never-a-component ref — distinguishing it from signal survival rate.
    dataset = _dataset(
        _chain("CR-1", CitationSource.CR, outcome=ThesisComponentOutcome.VALIDATED),
        _chain("CR-2", CitationSource.CR, outcome=ThesisComponentOutcome.INCONCLUSIVE),
        _chain("CR-3", CitationSource.CR, outcome=None),
    )
    metric = get_metric(metric_id_for("per_source_validation_rate", CitationSource.CR))
    assert metric is not None
    result = metric.compute(dataset, UNCONDITIONED)
    assert result.value == 1 / 2  # 1 validated of 2 resolved (CR-3 excluded)
    assert result.sample_size == 2


def test_synthesizer_recall_counts_dropped_refs_retrieved_by_decision_layer() -> None:
    # Recall denominator = uncited-by-synthesizer; numerator = those a decision
    # agent retrieved directly anyway.
    dataset = _dataset(
        _chain("SA-TECH-1", CitationSource.SA_TECH, synth=True, decision=True),  # not uncited
        _chain("QR-1", CitationSource.QR, synth=False, decision=True),  # dropped, recovered
        _chain("AR-1", CitationSource.AR, synth=False, decision=False),  # dropped, lost
    )
    metric = get_metric(MetricId("synthesizer_recall"))
    assert metric is not None
    result = metric.compute(dataset, UNCONDITIONED)
    assert result.value == 1 / 2  # of 2 uncited, 1 recovered
    assert result.sample_size == 2


def test_empty_sample_yields_none_and_insufficient() -> None:
    dataset = _dataset()  # no refs at all
    metric = get_metric(metric_id_for("synthesizer_citation_rate", CitationSource.SA_TECH))
    assert metric is not None
    result = metric.compute(dataset, UNCONDITIONED)
    assert result.value is None
    assert result.sample_size == 0
    assert result.insufficient_sample is True


def test_every_metric_id_distinct_and_per_source_complete() -> None:
    ids = [m.metric_id for m in METRICS]
    assert len(ids) == len(set(ids))  # no duplicate ids
    # 4 per-source metrics across 6 sources, plus 1 cross-source recall.
    assert len(ids) == 4 * len(CitationSource) + 1


def test_all_metrics_registered_and_discoverable() -> None:
    registered = {m.metric_id for m in list_metrics()}
    for metric in METRICS:
        assert metric.metric_id in registered
