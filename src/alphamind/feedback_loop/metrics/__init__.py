"""Deterministic metric-computation library + dynamic-discovery registry (ALP-882).

Every metric lives in its own module under ``feedback_loop/metrics/`` and exposes a
module-level ``METRICS`` tuple of :class:`~alphamind.feedback_loop.metrics.types.Metric`
descriptors. The registry walks the package, aggregates those tuples, and exposes
:func:`get_metric` / :func:`list_metrics`.

Discovery is dynamic (parent ALP-131 pre-resolved (G)): a new metric story (06\\*) adds a
module and self-registers with **zero edits** to this file — no shared ``__init__`` import
list to collide on across the parallel 06* wave.

:func:`get_metric` of an unregistered id returns ``None`` rather than raising, so a gated
metric's absence (e.g. PM-accuracy before the counterfactual-replay engine lands) lets a
consumer degrade gracefully instead of crashing the digest.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from alphamind.feedback_loop.metrics.types import MetricId

if TYPE_CHECKING:
    from alphamind.feedback_loop.metrics.types import Metric

# Module-level attribute every metric module exposes.
_METRICS_ATTR = "METRICS"

# Lazily-built id -> descriptor map; ``None`` until first discovery.
_registry: dict[MetricId, Metric] | None = None


def _discover() -> dict[MetricId, Metric]:
    """Walk this package's modules and aggregate their ``METRICS`` tuples.

    Skips private modules (leading underscore) other than the test-discovery
    probe, and skips this ``__init__`` itself. A duplicate ``metric_id`` across
    two modules is a programming error (the append-only id contract is violated)
    and raises :class:`ValueError`.
    """
    discovered: dict[MetricId, Metric] = {}
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        if name.startswith("_") and name != "_discovery_probe":
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        metrics = getattr(module, _METRICS_ATTR, ())
        for metric in metrics:
            if metric.metric_id in discovered:
                msg = (
                    f"duplicate metric_id {metric.metric_id!r} — "
                    f"MetricId is an append-only, unique contract"
                )
                raise ValueError(msg)
            discovered[metric.metric_id] = metric
    return discovered


def _ensure_registry() -> dict[MetricId, Metric]:
    global _registry
    if _registry is None:
        _registry = _discover()
    return _registry


def reset_registry_cache() -> None:
    """Clear the memoised registry so the next access re-scans the package.

    Used by tests that plant or remove a metric module at runtime; production
    code never needs it (the module set is fixed at import time).
    """
    global _registry
    _registry = None


def get_metric(metric_id: MetricId) -> Metric | None:
    """Return the descriptor for *metric_id*, or ``None`` if unregistered.

    Graceful degradation: an absent (e.g. gated) metric returns ``None`` rather
    than raising, so consumers skip it instead of failing.
    """
    return _ensure_registry().get(metric_id)


def list_metrics() -> tuple[Metric, ...]:
    """Return all registered metric descriptors, ordered by ``metric_id``."""
    return tuple(sorted(_ensure_registry().values(), key=lambda m: m.metric_id))


__all__ = [
    "get_metric",
    "list_metrics",
    "reset_registry_cache",
]
