"""Thin shim — defer to :mod:`alphamind.scripts.verify_state_persistence`.

Operator entry point so ``uv run python scripts/verify_state_persistence.py``
works without installing console-script entry points. The phase functions
(schema check, invocation-context round-trip, Phase 1 fill integration,
Phase 2 envelope writeback for both accepted and Layer-1-parse-failure paths,
SqlPortfolioStateRepository read parity) live in the
:mod:`alphamind.scripts.verify_state_persistence` module so unit tests under
``tests/scripts/`` can exercise them directly.

Usage::

    uv run python scripts/verify_state_persistence.py [--db-path PATH] [--output FORMAT]

See ``scripts/RUNBOOK_state_persistence.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_state_persistence import main

if __name__ == "__main__":
    sys.exit(main())
