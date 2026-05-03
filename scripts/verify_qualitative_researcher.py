"""Thin shim — defer to :mod:`alphamind.scripts.verify_qualitative_researcher`.

Operator entry point so ``uv run python scripts/verify_qualitative_researcher.py``
works without installing console-script entry points. The testable logic
lives in the ``alphamind.scripts.verify_qualitative_researcher`` module so
unit tests under ``tests/scripts/`` can exercise it without touching the SDK.

Usage:
  uv run python scripts/verify_qualitative_researcher.py [--db-path PATH] \
      [--archive-root DIR] [--as-of TS] [--last-invocation TS]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication).
  - Populated database with at least one recent distillation run (or the
    script falls back to a synthetic regime-label stub and surfaces the
    fallback in stdout).

See ``scripts/RUNBOOK_qualitative_researcher.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_qualitative_researcher import main

if __name__ == "__main__":
    sys.exit(main())
