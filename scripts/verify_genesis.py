"""Thin shim — defer to :mod:`alphamind.scripts.verify_genesis`.

Genesis-verify: assert the genesis-cutover runbook §8 first-run checklist on a
fresh, flat account — zero reconciliation alerts (by construction), a canary
entry fill that self-attributes via the broker-carried link, projection + Intent
both reflecting the canary, greeks in the ``position_greeks`` side table, the
options capital floor ``stop_limit`` resting at the broker, and no ``alp-…``
synthetic id anywhere. This is broker-boundary-redesign invariant 6 ("genesis is
clean") made executable; it gates the story-08 cutover.

Run it before declaring the cutover done. See ``scripts/RUNBOOK_genesis_verify.md``.

Usage:
  uv run python scripts/verify_genesis.py            # ephemeral self-contained smoke
  uv run python scripts/verify_genesis.py --db-path PATH
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_genesis import main

if __name__ == "__main__":
    sys.exit(main())
