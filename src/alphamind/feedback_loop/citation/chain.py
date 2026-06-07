"""Citation-chain metric cores — the cross-layer flagship (ALP-886 story 06d).

Five pure :class:`~alphamind.feedback_loop.metrics.types.Metric` cores over
:attr:`WindowDataset.refs` (``docs/design/feedback-loop.md`` § Citation-chain
metrics). Each traces upstream findings through the chain

    upstream finding → synthesizer brief → decision narrative → thesis component → resolution

and scores a per-source rate at one measurement point:

* **Synthesizer citation rate per source** — refs from X cited in synthesis / total from X.
* **Decision-layer citation rate per source** — refs from X cited in a decision
  narrative / total from X.
* **Signal survival rate per source** — refs from X that ended in a *validated*
  thesis component / total from X.
* **Per-source validation rate** — among refs from X that reached a *resolved*
  component, the fraction validated.
* **Synthesizer recall** (cross-source) — refs the synthesizer dropped but a
  decision agent retrieved directly / total uncited refs. Surfaces synthesizer
  drop errors.

Functional core / imperative shell (P1): every ``compute`` is pure over the
already-loaded dataset; the DB I/O lives in ``dataset._load_refs``. The
``feedback-loop-metric-cores-no-sqlalchemy`` import contract enforces the purity.
Each per-source metric is registered with a stable, append-only ``MetricId`` of
the form ``<metric>__<source>`` so the digest and validations reference it by
value; the registry discovers them via the module-level :data:`METRICS` tuple.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphamind.feedback_loop.citation.parser import CitationSource
from alphamind.feedback_loop.metrics.types import (
    Conditioning,
    Metric,
    MetricId,
    MetricResult,
    Window,
)
from alphamind.portfolio_state.records.theses import ThesisComponentOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from alphamind.feedback_loop.citation.parser import RefChain
    from alphamind.feedback_loop.dataset import WindowDataset

__all__ = ["METRICS", "metric_id_for"]


def metric_id_for(metric: str, source: CitationSource) -> MetricId:
    """Return the stable per-source ``MetricId`` (``<metric>__<source>``).

    The single place the per-source id grammar is encoded — the digest and any
    consumer derive the same id from the (metric, source) pair without
    hard-coding the string.
    """
    return MetricId(f"{metric}__{source.value}")


def _refs(dataset: WindowDataset) -> tuple[RefChain, ...]:
    return dataset.refs.citations


def _from_source(dataset: WindowDataset, source: CitationSource) -> list[RefChain]:
    return [chain for chain in _refs(dataset) if chain.ref.source is source]


def _ratio_result(metric_id: MetricId, numerator: int, denominator: int) -> MetricResult:
    """Build a rate :class:`MetricResult` (``numerator / denominator``).

    An empty denominator yields ``value=None`` and ``insufficient_sample=True`` —
    a rate is undefined with no refs from the source. The operator-tunable
    "needs N more observations" gate is a digest-layer concern (07a), applied
    over ``sample_size``; the pure core only reports the count.
    """
    if denominator == 0:
        return MetricResult(
            metric_id=metric_id,
            value=None,
            posterior_band=None,
            sample_size=0,
            insufficient_sample=True,
        )
    return MetricResult(
        metric_id=metric_id,
        value=numerator / denominator,
        posterior_band=None,
        sample_size=denominator,
        insufficient_sample=False,
    )


# ---------------------------------------------------------------------------
# Per-source rate cores
# ---------------------------------------------------------------------------


def _synthesizer_citation_rate(
    source: CitationSource,
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    metric_id = metric_id_for("synthesizer_citation_rate", source)

    def compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:  # noqa: ARG001 — per-source conditioning is the MetricId; no further slice
        chains = _from_source(dataset, source)
        cited = sum(1 for c in chains if c.cited_in_synthesis)
        return _ratio_result(metric_id, cited, len(chains))

    return compute


def _decision_layer_citation_rate(
    source: CitationSource,
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    metric_id = metric_id_for("decision_layer_citation_rate", source)

    def compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:  # noqa: ARG001 — per-source conditioning is the MetricId; no further slice
        chains = _from_source(dataset, source)
        cited = sum(1 for c in chains if c.cited_in_decision)
        return _ratio_result(metric_id, cited, len(chains))

    return compute


def _signal_survival_rate(
    source: CitationSource,
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    metric_id = metric_id_for("signal_survival_rate", source)

    def compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:  # noqa: ARG001 — per-source conditioning is the MetricId; no further slice
        chains = _from_source(dataset, source)
        survived = sum(
            1 for c in chains if c.thesis_component_outcome is ThesisComponentOutcome.VALIDATED
        )
        return _ratio_result(metric_id, survived, len(chains))

    return compute


def _per_source_validation_rate(
    source: CitationSource,
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    metric_id = metric_id_for("per_source_validation_rate", source)

    def compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:  # noqa: ARG001 — per-source conditioning is the MetricId; no further slice
        # Denominator is refs that reached a *resolved* component, not all refs:
        # "among theses citing X, validated / total" (doc § Citation-chain).
        resolved = [
            c for c in _from_source(dataset, source) if c.thesis_component_outcome is not None
        ]
        validated = sum(
            1 for c in resolved if c.thesis_component_outcome is ThesisComponentOutcome.VALIDATED
        )
        return _ratio_result(metric_id, validated, len(resolved))

    return compute


# ---------------------------------------------------------------------------
# Synthesizer recall (cross-source)
# ---------------------------------------------------------------------------

_SYNTHESIZER_RECALL_ID = MetricId("synthesizer_recall")


def _synthesizer_recall(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:  # noqa: ARG001 — whole-window cross-source metric; no conditioning slice
    """Refs the synthesizer dropped but a decision agent retrieved directly.

    Denominator is the *uncited* refs (not in the synthesizer brief); numerator
    is the subset of those a decision agent cited anyway. A high rate means the
    synthesizer is discarding findings the decision layer still needed.
    """
    uncited = [c for c in _refs(dataset) if not c.cited_in_synthesis]
    recovered = sum(1 for c in uncited if c.cited_in_decision)
    return _ratio_result(_SYNTHESIZER_RECALL_ID, recovered, len(uncited))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PER_SOURCE_CORES: tuple[
    tuple[str, Callable[[CitationSource], Callable[[WindowDataset, Conditioning], MetricResult]]],
    ...,
] = (
    ("synthesizer_citation_rate", _synthesizer_citation_rate),
    ("decision_layer_citation_rate", _decision_layer_citation_rate),
    ("signal_survival_rate", _signal_survival_rate),
    ("per_source_validation_rate", _per_source_validation_rate),
)


def _build_per_source_metrics() -> Iterable[Metric]:
    for name, factory in _PER_SOURCE_CORES:
        for source in CitationSource:
            yield Metric(
                metric_id=metric_id_for(name, source),
                po_type="outcome",
                default_window=Window.MONTHLY,
                supported_conditioning=(),
                compute=factory(source),
            )


METRICS: tuple[Metric, ...] = (
    *_build_per_source_metrics(),
    Metric(
        metric_id=_SYNTHESIZER_RECALL_ID,
        po_type="outcome",
        default_window=Window.MONTHLY,
        supported_conditioning=(),
        compute=_synthesizer_recall,
    ),
)
