"""Thin shim — defer to :mod:`alphamind.scripts.verify_regt_margin_attribution`.

Operator entry point so ``uv run python scripts/verify_regt_margin_attribution.py``
works without installing console-script entry points. The seeding, Phase 1
drive, rehydration, assembler invocation, and assertion functions live in
:mod:`alphamind.scripts.verify_regt_margin_attribution` so unit tests under
``tests/scripts/`` can exercise them directly.

Prerequisites: ``source .env`` before invocation (per memory
``feedback_handover_env_loading``). The script reads
``config/regt_margin_attribution.yaml`` via
:func:`load_regt_margin_attribution_config`; no other secrets are required.

Usage::

    uv run python scripts/verify_regt_margin_attribution.py \\
        [--db-path PATH] [--invocation-id ID] [--verbose]

See ``scripts/RUNBOOK_regt_margin_attribution.md`` for the operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_regt_margin_attribution import main

if __name__ == "__main__":
    sys.exit(main())
