---
name: feedback-loop-metric-core-conventions
description: How to add a metric module under feedback_loop/metrics/ — registry discovery, purity contract, MetricResult shape
metadata:
  type: project
---

Adding a decision-layer metric module (`src/alphamind/feedback_loop/metrics/<x>.py`):

- Expose a **module-level `METRICS` tuple** of `Metric` descriptors. The registry
  (`metrics/__init__.py::_discover`) walks the package via `pkgutil.iter_modules`,
  skips `_`-prefixed modules, and aggregates `METRICS`. **Do NOT edit `__init__.py`** —
  discovery is dynamic. Duplicate `metric_id` across modules raises ValueError.
- `Metric(metric_id, po_type, default_window, supported_conditioning, compute)`.
  `compute: (WindowDataset, Conditioning) -> MetricResult` is **pure** — no I/O, no
  sqlalchemy import (enforced by `.importlinter` contract
  `feedback-loop-metric-cores-no-sqlalchemy`).
- `MetricId` is a `NewType(str)` re-exported from `metrics.types` (minted in
  `feedback_loop.validation.records`). Construct via `MetricId("snake_case_id")`.
  Append-only, stable — `validations.watched_metric_ids` + the digest reference by value.
- `MetricResult(metric_id, value, posterior_band, sample_size, insufficient_sample)`.
  Process metrics: `posterior_band=None`. Empty sample: `value=None`,
  `insufficient_sample=True`. The threshold for `insufficient_sample` is
  operator-tunable — check how sibling stories source it (don't hard-code a number;
  see [[feedback-avoid-numeric-anchors]]).
- A distribution/histogram metric still returns a single `MetricResult` per
  (metric, conditioning) call — encode the multi-bin reading by minting one MetricId
  per bin (e.g. `pm_verdict_distribution__approve`) OR per design; check how the digest
  consumes it before deciding. Verify against story 07a's consumer if unsure.
- Tests live at `tests/feedback_loop/metrics/`; build `WindowDataset` fixtures by hand
  (no DB). `reset_registry_cache()` exists for tests that plant/remove modules.
