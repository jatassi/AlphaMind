"""Thin shim — defer to :mod:`alphamind.scripts.verify_counterfactual_replay_engine`.

Standalone end-to-end verify for the out-of-pipeline counterfactual replay
engine (ALP-129 / story 10). Seeds a controlled DB of PM-decision rows + bar
history + IV snapshots + strategist position state, runs
``replay_pending_proposals`` directly, and asserts each expected outcome against
the returned ``ReplayBatchResult`` and the persisted record set, then re-runs to
confirm idempotency. This engine is **out-of-pipeline**, so the central
``scripts/verify_debug_e2e.py`` gate does not cover it; the two scripts are
independent. See ``scripts/RUNBOOK_counterfactual_replay_engine.md``.

Usage:
  uv run python scripts/verify_counterfactual_replay_engine.py            # ephemeral
  uv run python scripts/verify_counterfactual_replay_engine.py --db-path PATH
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_counterfactual_replay_engine import main

if __name__ == "__main__":
    sys.exit(main())
