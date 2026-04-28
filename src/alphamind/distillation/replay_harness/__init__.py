"""Distillation replay harness package.

The harness re-runs the deterministic distillation layer against archived
fixtures under a candidate `config/distillation.yaml`, emitting a per-regime
flag-rate report. See `docs/design/02-distillation-layer/replay-harness.md`.
"""

from __future__ import annotations

from alphamind.distillation.replay_harness.version import HARNESS_VERSION

__all__ = ["HARNESS_VERSION"]
