"""Thin shim — defer to :mod:`alphamind.scripts.verify_domain_researchers`.

Operator entry point so ``uv run python scripts/verify_domain_researchers.py``
works without installing console-script entry points. The testable logic
lives in the ``alphamind.scripts.verify_domain_researchers`` module so unit
tests under ``tests/scripts/`` can exercise it without touching the SDK.

Usage:
  uv run python scripts/verify_domain_researchers.py [--db-path PATH] \
      [--archive-root DIR]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication).
  - Populated database with at least one recent distillation invocation.

See ``scripts/RUNBOOK_domain_researchers.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_domain_researchers import main

if __name__ == "__main__":
    sys.exit(main())
