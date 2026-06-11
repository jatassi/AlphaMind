"""Thin shim — defer to :mod:`alphamind.scripts.verify_feedback_loop`.

Standalone end-to-end verify for the out-of-pipeline feedback-loop spine
(ALP-131 / story 10). Runs ``alembic upgrade head`` on a fresh scratch DB, seeds
one controlled scenario (a CLOSED position + its linked ACTIVE thesis, ledger +
``POSITION_CLOSED`` exit, ``agent_calls`` telemetry, PM envelopes, a registered
validation), then drives the spine — resolver (ACTIVE→RESOLVED) → telemetry →
metric → digest + snapshot → validation register/evaluate → supersession →
retrospective — asserting each stage. The feedback loop is **out-of-pipeline**, so
the central ``scripts/verify/verify_debug_e2e.py`` gate does not cover it; the two scripts
are independent. See ``docs/runbooks/feedback-loop.md``.

Usage:
  uv run python scripts/verify/verify_feedback_loop.py            # ephemeral scratch DB
  uv run python scripts/verify/verify_feedback_loop.py --db-path PATH
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_feedback_loop import main

if __name__ == "__main__":
    sys.exit(main())
