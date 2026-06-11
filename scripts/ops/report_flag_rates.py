"""Thin shim — defer to :mod:`alphamind.scripts.report_flag_rates`.

Reports the empirical firing rate of every ``DISTILLATION_ANOMALY_FLAG`` over a
trailing window, grouped by ``(threshold_class, threshold_key)``, with the
calibration-state split, universe coverage, and a universe-wide-silence signal
the operator feeds into the Class A threshold-calibration review. Story 16 /
ALP-96 — operator review tool for structured triggers #1/#2 in
``threshold-calibration.md``.

Usage:
  uv run python scripts/ops/report_flag_rates.py \\
      [--window-days N] [--db-path PATH] [--threshold-class CLASS] \\
      [--ticker T] [--output text|json]
"""

from __future__ import annotations

import sys

from alphamind.scripts.report_flag_rates import main

if __name__ == "__main__":
    sys.exit(main())
