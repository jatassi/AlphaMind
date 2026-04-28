"""Thin shim — defer to :mod:`alphamind.scripts.verify_bootstrap_calibration_mix`.

Run after ``python -m alphamind.collector bootstrap`` and at least one
distillation orchestrator invocation; verifies the calibration-state
mix in ``distillation_ticker_baseline`` and ``distillation_pair_lag``
matches the warm-up estimate from
``docs/design/02-distillation-layer/threshold-calibration.md``
§ Warm-up duration estimate.

Usage:
  uv run python scripts/verify_bootstrap_calibration_mix.py [--db-path PATH]
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_bootstrap_calibration_mix import main

if __name__ == "__main__":
    sys.exit(main())
