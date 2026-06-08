"""Distillation-anomaly event detail payload (ALP-877).

Persists an :class:`AnomalyFlag` (from the distillation envelope) as an
activity-log entry so downstream consumers can query anomaly history without
re-parsing the invocation archive.

The ``AnomalySeverity`` type is re-declared here as a ``Literal`` to keep
``portfolio_state.events`` free of any import from ``alphamind.distillation``
(import direction: distillation is above portfolio_state in the layer order).
The string values are the canonical contract defined in
``docs/design/02-distillation-layer/external.md``; both sides must agree on
spelling. ``CalibrationState`` is imported from ``alphamind._kernel.calibration``
— the leaf vocabulary package that both distillation and portfolio_state depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alphamind._kernel.calibration import CalibrationState
from alphamind.portfolio_state.events.types import EventGroup, EventType

# ---------------------------------------------------------------------------
# Severity type — re-declared locally to avoid importing alphamind.distillation
# ---------------------------------------------------------------------------

AnomalySeverity = Literal[
    "investigate_now",
    "investigate_if_persists",
    "note_for_context",
]
"""Three-level severity taxonomy, matching ``distillation.output.AnomalySeverity``.

Re-declared here so ``portfolio_state.events`` remains free of any import from
``alphamind.distillation``. The string spellings are part of the persistence
contract and must stay in sync with the distillation producer side.
"""

_VALID_SEVERITIES: frozenset[str] = frozenset(
    {"investigate_now", "investigate_if_persists", "note_for_context"}
)


# ---------------------------------------------------------------------------
# Detail payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DistillationAnomalyFlagDetail:
    """Detail payload for ``DISTILLATION_ANOMALY_FLAG`` events.

    Mirrors the fields of :class:`~alphamind.distillation.output.AnomalyFlag`
    (``magnitude``, ``severity``) plus the source-block context
    (``calibration_state``, ``block_id``) and the ticker / threshold
    decomposition (``threshold_class``, ``threshold_key``, ``ticker``)
    enumerated in ALP-131 / ALP-96.

    The ``name`` field of the upstream ``AnomalyFlag`` is decomposed into
    ``threshold_class`` (the indicator category, e.g. ``"volume"``) and
    ``threshold_key`` (the specific threshold key within that category,
    e.g. ``"volume_anomaly_sigma"``). The emission hook (story 04b) is
    responsible for performing that decomposition before constructing this
    payload.

    ``ticker`` is ``None`` for cross-ticker (market-wide) anomalies where
    no single symbol is the primary subject.
    """

    threshold_class: str
    threshold_key: str
    magnitude: float
    severity: AnomalySeverity
    ticker: str | None
    calibration_state: CalibrationState
    block_id: str

    def __post_init__(self) -> None:
        if not self.threshold_class:
            msg = "threshold_class must be non-empty"
            raise ValueError(msg)
        if not self.threshold_key:
            msg = "threshold_key must be non-empty"
            raise ValueError(msg)
        if not self.block_id:
            msg = "block_id must be non-empty"
            raise ValueError(msg)
        if self.severity not in _VALID_SEVERITIES:
            msg = f"severity must be one of {sorted(_VALID_SEVERITIES)!r}; got {self.severity!r}"
            raise ValueError(msg)


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (
        EventType.DISTILLATION_ANOMALY_FLAG,
        DistillationAnomalyFlagDetail,
        EventGroup.DISTILLATION_ANOMALY,
    ),
]


__all__ = [
    "AnomalySeverity",
    "DistillationAnomalyFlagDetail",
]
