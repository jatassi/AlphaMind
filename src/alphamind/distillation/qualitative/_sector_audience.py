"""Sector → :class:`OutputAudience` resolution shared across qualitative loaders.

AlphaMind sectors in ``sector_classification.alphamind_sector`` are
``tech``, ``semis``, ``financials``, ``energy``. The output-envelope
audience taxonomy combines tech and semis under one researcher per
``docs/design/03-analysis-layer/domain-researchers/tech-semis.md``, so
the ticker → audience mapping resolves accordingly.

Tickers without a ``sector_classification`` row or with a sector outside
this map are omitted from the result; downstream compute treats them as
missing and skips block emission.
"""

from __future__ import annotations

from collections.abc import Sequence

from alphamind.distillation._repository import DistillationRepository
from alphamind.distillation.output import OutputAudience

_SECTOR_TO_AUDIENCE: dict[str, OutputAudience] = {
    "tech": OutputAudience.SECTOR_TECH_SEMIS,
    "semis": OutputAudience.SECTOR_TECH_SEMIS,
    "financials": OutputAudience.SECTOR_FINANCIALS,
    "energy": OutputAudience.SECTOR_ENERGY,
}


def sector_audience_map(
    repository: DistillationRepository, *, ticker_scope: Sequence[str]
) -> dict[str, OutputAudience]:
    """Resolve each ticker to its sector audience via the repository."""
    if not ticker_scope:
        return {}
    rows = repository.load_sector_classifications(tickers=tuple(ticker_scope))
    out: dict[str, OutputAudience] = {}
    for ticker, row in rows.items():
        audience = _SECTOR_TO_AUDIENCE.get(row.alphamind_sector)
        if audience is not None:
            out[ticker] = audience
    return out


__all__ = ["sector_audience_map"]
