"""Thin shim — defer to :mod:`alphamind.scripts.verify_guardrail_enforcement`.

Operator entry point so ``uv run python scripts/verify_guardrail_enforcement.py``
works without installing console-script entry points. The phase functions live
in the :mod:`alphamind.scripts.verify_guardrail_enforcement` module so unit
tests under ``tests/scripts/`` can exercise them directly.

Usage::

    uv run python scripts/verify_guardrail_enforcement.py [--db-path PATH] [--output FORMAT]

See ``scripts/RUNBOOK_guardrail_enforcement.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_guardrail_enforcement import main

if __name__ == "__main__":
    sys.exit(main())
