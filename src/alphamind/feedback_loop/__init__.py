"""Feedback-loop analytics spine — month-over-month system-improvement substrate.

Pairs the system's reasoning artifacts with the outcomes they produced and computes
deterministic, conditionable metrics over that pairing. Read-only over trading state:
the package measures and proposes, never mutating live trading-state records.

Subpackages (filled by later ALP-131 stories):

* ``metrics/`` — the deterministic metric-computation library + WindowDataset loader.
* ``citation/`` — reference-ID / citation-chain analysis.
* ``digest/`` — weekly-digest generation.
* ``validation/`` — confounder-managed validation-discipline logic.
* ``retrospective/`` — the retrospective (periodic LLM-driven review) data layer.
"""

from __future__ import annotations
