"""Thin shim — defer to :mod:`alphamind.scripts.verify_broker_adapter`.

Operator entry point so ``uv run python scripts/verify_broker_adapter.py`` works
without installing console-script entry points. The phase functions live in
the :mod:`alphamind.scripts.verify_broker_adapter` module so unit tests under
``tests/scripts/`` can exercise them directly.

Usage::

    uv run python scripts/verify_broker_adapter.py [--mode paper|live] [--phase NAME] [--verbose]

See ``scripts/RUNBOOK_broker_adapter.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_broker_adapter import main

if __name__ == "__main__":
    sys.exit(main())
