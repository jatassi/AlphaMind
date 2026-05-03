"""Thin shim — defer to :mod:`alphamind.scripts.verify_adaptive_researcher`.

Operator entry point so ``uv run python scripts/verify_adaptive_researcher.py``
works without installing console-script entry points. The testable logic
lives in the ``alphamind.scripts.verify_adaptive_researcher`` module so
unit tests under ``tests/scripts/`` can exercise it without touching the SDK.

Usage:
  uv run python scripts/verify_adaptive_researcher.py [--db-path PATH] \
      [--archive-root DIR] [--invocation-id INV] [--as-of TS]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication).
  - Populated database accessible at the resolved path (the harness's
    in-process tools read the news, prediction-market, and macro tables
    via the SQLAlchemy session).
  - The recorded upstream-brief fixtures under
    ``tests/analysis/adaptive_research/fixtures/`` are committed and
    importable from this process.

See ``scripts/RUNBOOK_adaptive_researcher.md`` for the operator runbook.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The src module's lazy fixture loader does ``from tests.analysis... import``;
# ``tests/`` is not a regular package on ``sys.path`` under ``uv run python
# scripts/...`` (only ``src/`` is). Insert the repo root so the lazy import
# resolves without requiring the operator to ``export PYTHONPATH=.``.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from alphamind.scripts.verify_adaptive_researcher import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
