"""Thin shim — defer to :mod:`alphamind.scripts.verify_oms_commands`.

Operator entry point so ``uv run python scripts/verify_oms_commands.py`` works
without installing console-script entry points. The phase functions live in
the :mod:`alphamind.scripts.verify_oms_commands` module so unit tests under
``tests/scripts/`` can exercise them directly.

Usage::

    uv run python scripts/verify_oms_commands.py [--db-path PATH] [--output FORMAT]

See ``scripts/RUNBOOK_oms_commands.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_oms_commands import main

if __name__ == "__main__":
    sys.exit(main())
