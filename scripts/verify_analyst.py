"""Thin shim — defer to :mod:`alphamind.scripts.verify_analyst`.

Operator entry point so ``uv run python scripts/verify_analyst.py`` works
without installing console-script entry points. The testable logic — the
verdict rubric, the synthesizer-archive reader, and the canonical
fixture builders — lives in the ``alphamind.scripts.verify_analyst``
module so unit tests under ``tests/scripts/`` can exercise it without
touching the real Claude Agent SDK.

Usage:
  uv run python scripts/verify_analyst.py \
      --archive-root DIR --synthesizer-invocation-id INV \
      [--save-fixtures] [--fixtures-dir DIR]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication).
  - A prior ``verify_synthesizer.py --archive-root <DIR> --invocation-id <INV>``
    run that produced ``analysis/synthesizer/response.md`` and
    ``stage_artifacts/retrieval_store.json`` under that archive root.

See ``scripts/RUNBOOK_analyst.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_analyst import main

if __name__ == "__main__":
    sys.exit(main())
