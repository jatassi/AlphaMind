"""Thin shim — defer to :mod:`alphamind.scripts.verify_regime_transition`.

Reads ``distillation_regime_state`` for the last ``--lookback-days N``
(default 7) and asserts the regime state-machine invariants from story
09 hold.

Usage:
  uv run python scripts/verify_regime_transition.py [--db-path PATH] \\
      [--lookback-days N]
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_regime_transition import main

if __name__ == "__main__":
    sys.exit(main())
