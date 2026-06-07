"""Dynamic-discovery registry for metric descriptors (ALP-882 story 05).

The registry walks every module under ``feedback_loop/metrics/`` looking for a
module-level ``METRICS`` tuple, aggregates them, and exposes ``get_metric`` /
``list_metrics``. ``get_metric`` of an unregistered id returns ``None`` so a
gated metric's absence (e.g. PM-accuracy before ALP-129 lands) never crashes a
consumer — it degrades.

Discovery is exercised by writing a real metric module into the ``metrics``
package directory for the duration of a test, then forcing a re-scan. This is
the genuine discovery path — no metric module is patched into ``sys.modules`` by
hand.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

import alphamind.feedback_loop.metrics as metrics_pkg
from alphamind.feedback_loop.metrics import get_metric, list_metrics
from alphamind.feedback_loop.metrics.types import MetricId

_METRICS_DIR = Path(metrics_pkg.__file__).parent


@pytest.fixture()
def temp_metric_module() -> Iterator[MetricId]:
    """Write a throwaway metric module into the metrics package, then remove it.

    Yields the ``MetricId`` the module registers so the test can assert the
    registry discovered it. The module is deleted and the registry cache reset
    on teardown so the planted metric never leaks into other tests.
    """
    module_path = _METRICS_DIR / "_discovery_probe.py"
    module_path.write_text(
        textwrap.dedent(
            '''\
            """Throwaway metric module planted by the registry discovery test."""

            from __future__ import annotations

            from alphamind.feedback_loop.metrics.types import (
                Conditioning,
                Metric,
                MetricId,
                MetricResult,
                Window,
            )
            from alphamind.feedback_loop.dataset import WindowDataset


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
    metrics_pkg.reset_registry_cache()
    try:
        yield MetricId("discovery_probe")
    finally:
        module_path.unlink(missing_ok=True)
        metrics_pkg.reset_registry_cache()


class TestRegistry:
    def test_unknown_id_returns_none(self) -> None:
        assert get_metric(MetricId("no_such_metric_xyz")) is None

    def test_list_metrics_returns_tuple(self) -> None:
        assert isinstance(list_metrics(), tuple)

    def test_discovers_planted_metric(self, temp_metric_module: MetricId) -> None:
        descriptor = get_metric(temp_metric_module)
        assert descriptor is not None
        assert descriptor.metric_id == temp_metric_module
        assert descriptor.po_type == "process"

    def test_planted_metric_listed(self, temp_metric_module: MetricId) -> None:
        assert temp_metric_module in {m.metric_id for m in list_metrics()}

    def test_metric_absent_after_teardown(self) -> None:
        """Confirms the planted module does not leak between tests."""
        assert get_metric(MetricId("discovery_probe")) is None
