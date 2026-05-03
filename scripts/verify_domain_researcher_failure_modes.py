"""Thin shim — defer to :mod:`alphamind.scripts.verify_domain_researcher_failure_modes`.

Operator entry point so ``uv run python scripts/verify_domain_researcher_failure_modes.py``
works without installing console-script entry points. The testable logic
lives in the ``alphamind.scripts.verify_domain_researcher_failure_modes``
module so unit tests under ``tests/scripts/`` can exercise it without
spawning a subprocess.

Usage:
  uv run python scripts/verify_domain_researcher_failure_modes.py [--archive-root DIR]

See ``scripts/RUNBOOK_domain_researchers.md`` for the operator workflow.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_domain_researcher_failure_modes import main

if __name__ == "__main__":
    sys.exit(main())
