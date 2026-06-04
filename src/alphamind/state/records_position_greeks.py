"""Typed record for the ``position_greeks`` side table (ALP-843 / W0a).

The greeks are the lone computed decoration in the redesign — held in a
**single-writer monitor-owned side table**, never an RMW on the ``positions``
row (ADR-0005). Keying by ``position_id`` and isolating the columns here is what
makes the monitor's greeks refresh a clean append/upsert on its *own* table
rather than a cross-process read-modify-write on the pipeline-owned positions
row (the second-writer pattern that generated ALP-824).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PositionGreeksRecord(BaseModel):
    """Frozen per-position greeks snapshot. Single-writer = monitor.

    ``iv`` is the implied volatility used for the computation. ``updated_at``
    records when the monitor last refreshed the row.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float | None
    updated_at: datetime


__all__ = ["PositionGreeksRecord"]
