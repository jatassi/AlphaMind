"""Thin shim — defer to :mod:`alphamind.scripts.verify_proposal_pre_processor`.

Operator entry point so ``uv run python scripts/verify_proposal_pre_processor.py``
works without installing console-script entry points. The testable logic —
the verdict rubric, the fixture builders, and the four scenario runners —
lives in the ``alphamind.scripts.verify_proposal_pre_processor`` module so
unit tests under ``tests/scripts/`` can exercise it without touching the
real Claude Agent SDK.

Usage::

    uv run python scripts/verify_proposal_pre_processor.py [--scenario SCENARIO]

where SCENARIO is one of ``normal``, ``halt``, ``emergency``,
``normal_with_breach``, or ``all`` (default).

Prerequisites:
  - ``tests/fixtures/decision/analyst/normal.json`` and ``halt.json`` must
    exist. If missing, run ``scripts/verify_analyst.py --save-fixtures`` first.

See ``scripts/RUNBOOK_proposal_pre_processor.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_proposal_pre_processor import main

if __name__ == "__main__":
    sys.exit(main())
