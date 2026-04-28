"""Thin shim — defer to :mod:`alphamind.scripts.verify_distillation`.

The testable logic lives in the ``alphamind.scripts.verify_distillation``
module; this script is the operator entry point so
``uv run python scripts/verify_distillation.py`` works without
installing console-script entry points.

Usage:
  uv run python scripts/verify_distillation.py [--db-path PATH] \\
      [--archive-root DIR] [--freshness-window-minutes N]

See ``docs/implementation/02-distillation-layer/VERIFICATION.md`` for
the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_distillation import main

if __name__ == "__main__":
    sys.exit(main())
