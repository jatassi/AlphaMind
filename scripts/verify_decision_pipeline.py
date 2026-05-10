"""Thin shim — defer to :mod:`alphamind.scripts.verify_decision_pipeline`.

Operator entry point so ``uv run python scripts/verify_decision_pipeline.py``
works alongside the canonical ``uv run python -m
alphamind.scripts.verify_decision_pipeline`` invocation. The testable
logic — the auth check, the fixture-builder helpers, the result-
validation predicate, and the result serializer — lives in the
``alphamind.scripts.verify_decision_pipeline`` module so unit tests
under ``tests/scripts/`` can exercise it without touching the real
Claude Agent SDK.

Usage:
  uv run python scripts/verify_decision_pipeline.py \
      --archive-root DIR \
      [--scenario normal]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication).
  - ``uv sync`` completed.

See ``scripts/RUNBOOK_decision_pipeline.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_decision_pipeline import main

if __name__ == "__main__":
    sys.exit(main())
