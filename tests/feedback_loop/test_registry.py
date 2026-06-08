"""Dynamic-discovery registry for metric descriptors (ALP-882 story 05).

The registry walks every module under ``feedback_loop/metrics/`` looking for a
module-level ``METRICS`` tuple, aggregates them, and exposes ``get_metric`` /
``list_metrics``. ``get_metric`` of an unregistered id returns ``None`` so a
gated metric's absence (e.g. PM-accuracy before ALP-129 lands) never crashes a
consumer — it degrades.

Discovery is exercised by planting a real metric module on a per-test temporary
directory appended to the package's ``__path__``. Using ``tmp_path`` (not the
shared ``src/`` tree) keeps the probe isolated per test, so the suite is safe
under ``pytest -n auto`` — no two xdist workers ever share the planted file, and
nothing leaks into the real package between tests.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

import alphamind.feedback_loop.metrics as metrics_pkg
from alphamind.feedback_loop.metrics import get_metric, list_metrics
from alphamind.feedback_loop.metrics.types import MetricId

_PROBE_ID = "discovery_probe"
_PROBE_MODULE = "discovery_probe"


@pytest.fixture()
def planted_metric(tmp_path: Path) -> Iterator[MetricId]:
    """Plant a metric module on a tmp dir added to the package ``__path__``.

    Yields the ``MetricId`` the module registers. On teardown the tmp dir is
    removed from ``__path__``, the imported probe module is dropped from
    ``sys.modules``, and the registry cache is reset — so the probe never leaks
    into another test.
    """
    (tmp_path / f"{_PROBE_MODULE}.py").write_text(
        textwrap.dedent(
            '''\
            """Throwaway metric module planted by the registry discovery test."""

            from __future__ import annotations

            from alphamind.feedback_loop.dataset import WindowDataset
            from alphamind.feedback_loop.metrics.types import (
                Conditioning,
                Metric,
                MetricId,
                MetricResult,
                Window,
            )


            def _compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
                return MetricResult(
                    metric_id=MetricId("discovery_probe"),
                    value=1.0,
                    posterior_band=None,
                    sample_size=0,
                    insufficient_sample=True,
                )


            METRICS = (
                Metric(
                    metric_id=MetricId("discovery_probe"),
                    po_type="process",
                    default_window=Window.WEEKLY,
                    supported_conditioning=(),
                    compute=_compute,
                ),
            )
            '''
        ),
        encoding="utf-8",
    )
    metrics_pkg.__path__.append(str(tmp_path))
    metrics_pkg.reset_registry_cache()
    try:
        yield MetricId(_PROBE_ID)
    finally:
        metrics_pkg.__path__.remove(str(tmp_path))
        sys.modules.pop(f"{metrics_pkg.__name__}.{_PROBE_MODULE}", None)
        metrics_pkg.reset_registry_cache()


class TestRegistry:
    def test_unknown_id_returns_none(self) -> None:
        assert get_metric(MetricId("no_such_metric_xyz")) is None

    def test_list_metrics_returns_tuple(self) -> None:
        assert isinstance(list_metrics(), tuple)

    def test_discovers_planted_metric(self, planted_metric: MetricId) -> None:
        descriptor = get_metric(planted_metric)
        assert descriptor is not None
        assert descriptor.metric_id == planted_metric
        assert descriptor.po_type == "process"

    def test_planted_metric_listed(self, planted_metric: MetricId) -> None:
        assert planted_metric in {m.metric_id for m in list_metrics()}
