"""Thin shim — defer to :mod:`alphamind.scripts.verify_synthesizer`.

Operator entry point so ``uv run python scripts/verify_synthesizer.py``
works without installing console-script entry points. The testable
logic — the reference-coverage classifier and the verdict rubric —
lives in the ``alphamind.scripts.verify_synthesizer`` module so unit
tests under ``tests/scripts/`` can exercise it without touching the
real Claude Agent SDK.

Usage:
  uv run python scripts/verify_synthesizer.py [--archive-root DIR] \
      [--as-of TS] [--invocation-id INV]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication). The
    script surfaces a missing token as a clean ``SDKFailure``.

See ``scripts/RUNBOOK_synthesizer.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_synthesizer import main

if __name__ == "__main__":
    sys.exit(main())
