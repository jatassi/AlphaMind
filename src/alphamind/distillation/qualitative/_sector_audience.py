"""Sector → :class:`OutputAudience` resolution for qualitative loaders.

Reuses the canonical :data:`AUDIENCE_BY_SECTOR` mapping from
:mod:`alphamind.distillation.q1.output_blocks` so a fifth sector ever
added there propagates here without code change. Tickers without a
``sector_classification`` row or with a sector outside the map are
omitted; downstream compute treats them as missing and skips block
emission.
"""

from __future__ import annotations

from collections.abc import Sequence

from alphamind.distillation._repository import DistillationRepository
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q1.output_blocks import AUDIENCE_BY_SECTOR


def sector_audience_map(
    repository: DistillationRepository, *, ticker_scope: Sequence[str]
) -> dict[str, OutputAudience]:
    """Resolve each ticker to its sector audience via the repository."""
    if not ticker_scope:
        return {}
    rows = repository.load_sector_classifications(tickers=tuple(ticker_scope))
    out: dict[str, OutputAudience] = {}
    for ticker, row in rows.items():
        audience = AUDIENCE_BY_SECTOR.get(row.alphamind_sector)
        if audience is not None:
            out[ticker] = audience
    return out


__all__ = ["sector_audience_map"]
