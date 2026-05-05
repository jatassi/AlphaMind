"""Thin shim — defer to :mod:`alphamind.scripts.verify_pm`.

Operator entry point so ``uv run python scripts/verify_pm.py`` works
without installing console-script entry points. The testable logic — the
verdict rubric, the synthesizer-archive reader, and the four in-code
scenario builders — lives in the ``alphamind.scripts.verify_pm`` module
so unit tests under ``tests/scripts/`` can exercise it without touching
the real Claude Agent SDK.

Usage:
  uv run python scripts/verify_pm.py \
      --archive-root DIR --synthesizer-invocation-id INV \
      [--save-fixtures] [--fixtures-dir DIR] \
      [--scenario {normal,halt,emergency,synchronous_rejection,all}]

Prerequisites:
  - ``CLAUDE_CODE_OAUTH_TOKEN`` set in the environment (see
    ``docs/architecture/llm-integration.md`` § Authentication).
  - A prior ``verify_synthesizer.py --archive-root <DIR> --invocation-id <INV>``
    run that produced ``analysis/synthesizer/response.md`` and
    ``stage_artifacts/retrieval_store.json`` under that archive root.
  - The four pre-processor fixtures must exist under
    ``tests/fixtures/decision/proposal_pre_processor/`` —
    ``normal.json``, ``halt.json``, ``emergency.json``,
    ``normal_with_breach.json``. Run
    ``scripts/verify_proposal_pre_processor.py --save-fixtures`` if any
    are missing.

See ``scripts/RUNBOOK_pm.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_pm import main

if __name__ == "__main__":
    sys.exit(main())
