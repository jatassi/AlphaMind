"""Thin shim — defer to :mod:`alphamind.scripts.verify_pipeline_scheduler`.

Operator entry point so ``uv run python scripts/verify_pipeline_scheduler.py``
works alongside the canonical ``uv run python -m
alphamind.scripts.verify_pipeline_scheduler`` invocation. The testable
logic — the auth check, the DB schema check, the row-population
predicate, the activity-log check, the archive-directory check, and the
vocabulary check — lives in the
:mod:`alphamind.scripts.verify_pipeline_scheduler` module so unit tests
under :mod:`tests.scripts.test_verify_pipeline_scheduler` can exercise
them without driving a real ``--once`` invocation.

Usage:
  set -a && source .env && set +a && \\
      uv run python scripts/verify_pipeline_scheduler.py \\
          --archive-root ~/AlphaMind/archive \\
          [--run-type market_hours_rolling] \\
          [--mode paper]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication).
  - ``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET`` set for Phase 1.
  - ``uv sync`` completed.
  - Paper DB migrated to the current head.

See ``scripts/RUNBOOK_pipeline_scheduler.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_pipeline_scheduler import main

if __name__ == "__main__":
    sys.exit(main())
