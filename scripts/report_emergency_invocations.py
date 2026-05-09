"""Thin shim — defer to :mod:`alphamind.scripts.report_emergency_invocations`.

Lists every emergency invocation in the window, classifies each by
downstream consequence, and surfaces the per-trigger-reason
false-positive rate the operator uses to decide whether to disable
``regime_skip_emergency_trigger`` or re-tune any other emergency-trigger
logic. Story 15 / ALP-99 — operator review tool for structured trigger
#3 in ``threshold-calibration.md``.

Usage:
  uv run python scripts/report_emergency_invocations.py \\
      [--db-path PATH] [--window-days N] [--output text|json]
"""

from __future__ import annotations

import sys

from alphamind.scripts.report_emergency_invocations import main

if __name__ == "__main__":
    sys.exit(main())
