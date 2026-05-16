"""Thin orchestration shim for the Q7 correlation regime change blocks (ALP-486).

The pure compute lives in :mod:`.correlation_regime_change_compute`; the
session-bound IO shell helpers live in :mod:`._loaders` (the ``news_articles``
scan that resolves the ``qualifying_news_present`` flag is there). This
module preserves the legacy session-accepting public API and re-exports
the ``CorrelationRegimeChangeConfig`` alias plus the
``NARRATIVE_LAG_REGIME_TAGS`` constant.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._loaders import (
    NARRATIVE_LAG_REGIME_TAGS,
    _load_correlation_regime_change_blocks,
)
from alphamind.distillation.q7.correlation_regime_change_compute import (
    CorrelationRegimeChangeParameters,
    compute_correlation_regime_change_pure,
)

# Back-compat alias: legacy callers import ``CorrelationRegimeChangeConfig``.
CorrelationRegimeChangeConfig = CorrelationRegimeChangeParameters


def compute_correlation_regime_change(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    as_of: datetime,
    config: CorrelationRegimeChangeParameters,
) -> list[OutputBlock]:
    """Compute correlation-breakdown / dispersion / narrative-lag blocks.

    Session-accepting thin shim that delegates to the pure compute under
    pre-loaded universe returns and the loader-resolved
    ``qualifying_news_present`` flag.
    """
    blocks = _load_correlation_regime_change_blocks(
        session,
        universe_tickers=universe_tickers,
        as_of=as_of,
        params=config,
    )
    return list(blocks)


__all__ = [
    "NARRATIVE_LAG_REGIME_TAGS",
    "CorrelationRegimeChangeConfig",
    "CorrelationRegimeChangeParameters",
    "compute_correlation_regime_change",
    "compute_correlation_regime_change_pure",
]
