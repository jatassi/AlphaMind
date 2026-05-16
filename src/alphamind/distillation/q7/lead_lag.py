"""Thin orchestration shim for the Q7 lead-lag blocks (ALP-486).

The pure compute (plus the :class:`LeadLagPair` dataclass) lives in
:mod:`.lead_lag_compute`; the session-bound IO shell helpers live in
:mod:`._loaders`. This module preserves the legacy session-accepting
public API.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._loaders import _load_lead_lag_blocks
from alphamind.distillation.q7.lead_lag_compute import (
    LEAD_LAG_LOOKBACK_DEFAULT_DAYS,
    LeadLagPair,
    LeadLagPairInputs,
    compute_lead_lag_pure,
)


def compute_lead_lag(
    session: Session,
    *,
    pairs: Sequence[LeadLagPair],
    as_of: datetime,
    overdue_lead_sigma: float,
    lookback_window_days: int = LEAD_LAG_LOOKBACK_DEFAULT_DAYS,
) -> list[OutputBlock]:
    """Compute per-pair lead-lag overdue and inversion blocks.

    Session-accepting thin shim that delegates to the pure compute under
    pre-loaded recent returns and persisted ``distillation_pair_lag``
    rows.
    """
    blocks = _load_lead_lag_blocks(
        session,
        pairs=pairs,
        as_of=as_of,
        overdue_lead_sigma=overdue_lead_sigma,
        lookback_window_days=lookback_window_days,
    )
    return list(blocks)


__all__ = [
    "LEAD_LAG_LOOKBACK_DEFAULT_DAYS",
    "LeadLagPair",
    "LeadLagPairInputs",
    "compute_lead_lag",
    "compute_lead_lag_pure",
]
