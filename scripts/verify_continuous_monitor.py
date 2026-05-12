"""Thin shim — defer to :mod:`alphamind.scripts.verify_continuous_monitor`.

Operator entry point so ``uv run python scripts/verify_continuous_monitor.py``
works alongside the canonical ``uv run python -m
alphamind.scripts.verify_continuous_monitor`` invocation. The testable
logic — the per-scenario kernel drives and the runner aggregation — lives
in the ``alphamind.scripts.verify_continuous_monitor`` module so unit tests
under ``tests/scripts/`` can exercise it without going through the shim.

Usage:
  uv run python scripts/verify_continuous_monitor.py [--output FORMAT]

Prerequisites:
  - ``uv sync`` completed.

The verify script exercises every scenario against in-memory fixtures —
no live websocket calls, no live broker submissions, no live vendor calls.
Safe to run in CI. See ``scripts/RUNBOOK_continuous_monitor.md`` for the
operator runbook.
"""

from __future__ import annotations

import sys

from alphamind.scripts.verify_continuous_monitor import main

if __name__ == "__main__":
    sys.exit(main())
