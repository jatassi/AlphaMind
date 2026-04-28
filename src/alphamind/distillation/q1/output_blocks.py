"""Q1 output-block assembly — story 02-distillation/08a.

Produces :class:`alphamind.distillation.output.OutputBlock` envelopes for
each of the six Q1 indicator groups, applying the per-ticker payload
convention pinned across stories 08a-f.

The convention (re-stated for the call site):

- One ``OutputBlock`` per ``(sector_audience, indicator_group)`` — not one per
  ticker.
- ``OutputBlock.audience = frozenset({that_sector})``.
- ``OutputBlock.payload = {"per_ticker": {ticker: {...}, ...}}``, sorted by
  ticker key via ``dict(sorted(per_ticker.items()))``.
- ``block_id`` namespace ``q1.*`` per the documented six groups.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock

# ---------------------------------------------------------------------------
# Block-id namespace — one per indicator group per story 08a
# ---------------------------------------------------------------------------

BLOCK_ID_TECHNICALS: str = "q1.technicals"
BLOCK_ID_VOLUME_PROFILE: str = "q1.volume_profile"
BLOCK_ID_GAP: str = "q1.gap"
BLOCK_ID_RELATIVE_PERFORMANCE: str = "q1.relative_performance"
BLOCK_ID_TREND_STATE: str = "q1.trend_state"
BLOCK_ID_DIVERGENCE_FLAGS: str = "q1.divergence_flags"


_VALID_BLOCK_IDS: frozenset[str] = frozenset(
    {
        BLOCK_ID_TECHNICALS,
        BLOCK_ID_VOLUME_PROFILE,
        BLOCK_ID_GAP,
        BLOCK_ID_RELATIVE_PERFORMANCE,
        BLOCK_ID_TREND_STATE,
        BLOCK_ID_DIVERGENCE_FLAGS,
    }
)


# ---------------------------------------------------------------------------
# Sector → audience mapping
# ---------------------------------------------------------------------------


AUDIENCE_BY_SECTOR: Mapping[str, OutputAudience] = {
    "tech": OutputAudience.SECTOR_TECH_SEMIS,
    "semis": OutputAudience.SECTOR_TECH_SEMIS,
    "financials": OutputAudience.SECTOR_FINANCIALS,
    "energy": OutputAudience.SECTOR_ENERGY,
}
"""``sector_classification.alphamind_sector`` → :class:`OutputAudience` mapping.

Tech and semis share the ``SECTOR_TECH_SEMIS`` audience because the
domain researcher is the same agent
(``docs/design/03-analysis-layer/domain-researchers/tech-semis.md``).
Financials and energy each have their own dedicated audience.
"""


def audience_for_sector(alphamind_sector: str) -> OutputAudience:
    """Return the :class:`OutputAudience` for a ``sector_classification.alphamind_sector`` value.

    Raises :class:`ValueError` for an unrecognized sector — the storage
    layer's CHECK constraint accepts only the four documented values, so an
    unknown value here is a bug at the call site.
    """
    try:
        return AUDIENCE_BY_SECTOR[alphamind_sector]
    except KeyError as exc:
        raise ValueError(
            f"audience_for_sector: unknown alphamind_sector {alphamind_sector!r}; "
            f"expected one of {sorted(AUDIENCE_BY_SECTOR)}"
        ) from exc


# ---------------------------------------------------------------------------
# build_q1_block — the per-block factory
# ---------------------------------------------------------------------------


def build_q1_block(
    *,
    block_id: str,
    sector: str,
    per_ticker: Mapping[str, Mapping[str, Any]],
    freshness_ts: datetime,
    calibration_state: CalibrationState,
    bootstrap_reason: str | None,
    anomaly_flags: tuple[AnomalyFlag, ...] = (),
    regime_context: str | None = None,
) -> OutputBlock:
    """Build a Q1 :class:`OutputBlock` with the per-ticker payload convention.

    The payload is wrapped as ``{"per_ticker": {<sorted ticker map>}}`` so
    consumers reach indicator values via a fixed key path regardless of
    which Q1 group the block belongs to.

    Raises :class:`ValueError` for an unknown ``block_id`` (bug at the call
    site — the namespace is closed).
    """
    if block_id not in _VALID_BLOCK_IDS:
        raise ValueError(
            f"build_q1_block: unknown block_id {block_id!r}; "
            f"expected one of {sorted(_VALID_BLOCK_IDS)}"
        )
    audience = frozenset({audience_for_sector(sector)})
    sorted_per_ticker: dict[str, Mapping[str, Any]] = dict(sorted(per_ticker.items()))
    payload = {"per_ticker": sorted_per_ticker}
    return OutputBlock(
        block_id=block_id,
        audience=audience,
        freshness_ts=freshness_ts,
        calibration_state=calibration_state,
        bootstrap_reason=bootstrap_reason,
        payload=payload,
        anomaly_flags=anomaly_flags,
        regime_context=regime_context,
    )


__all__ = [
    "AUDIENCE_BY_SECTOR",
    "BLOCK_ID_DIVERGENCE_FLAGS",
    "BLOCK_ID_GAP",
    "BLOCK_ID_RELATIVE_PERFORMANCE",
    "BLOCK_ID_TECHNICALS",
    "BLOCK_ID_TREND_STATE",
    "BLOCK_ID_VOLUME_PROFILE",
    "audience_for_sector",
    "build_q1_block",
]
