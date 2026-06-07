"""Distillation anomaly-flag → activity_log mapping + emission (ALP-881 / story 04b).

Two layers under test:

- The pure mapper :func:`anomaly_summary_to_activity_log_entry` in
  ``distillation.aggregation`` — turns one :class:`AnomalySummary` into a typed
  ``DISTILLATION_ANOMALY_FLAG`` :class:`ActivityLogEntry`, populating the detail
  payload from the originating flag/summary and resolving
  ``threshold_class`` / ``threshold_key`` via ``resolve_flag_taxonomy``.
- The orchestrator emission hook — a distillation run that produces N flags
  writes N anomaly-typed rows, queryable by the anomaly ``EventType`` (the
  shape ALP-96's reporter consumes), each carrying the running invocation FK.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.calibration import CalibrationState
from alphamind.distillation.aggregation import (
    AnomalySummary,
    anomaly_summary_to_activity_log_entry,
)
from alphamind.distillation.output import AnomalyFlag, OutputAudience
from alphamind.portfolio_state.events import (
    DistillationAnomalyFlagDetail,
    EventGroup,
    EventSource,
    EventType,
)

_AS_OF = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _summary(
    *,
    name: str,
    magnitude: float = 3.5,
    severity: str = "investigate_now",
    block_id: str = "q1.volume.NVDA",
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
) -> AnomalySummary:
    return AnomalySummary(
        flag=AnomalyFlag(name=name, magnitude=magnitude, severity=severity),  # type: ignore[arg-type]
        source_block_id=block_id,
        audiences=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        flagged_at=_AS_OF,
        calibration_state=calibration_state,
    )


def test_mapper_builds_distillation_anomaly_flag_entry() -> None:
    """A market-wide flag maps to a ``DISTILLATION_ANOMALY_FLAG`` entry with the
    detail populated from the flag/summary and the taxonomy resolved."""
    summary = _summary(name="volume_anomaly", magnitude=4.2, severity="investigate_now")

    entry = anomaly_summary_to_activity_log_entry(
        summary, invocation_id="inv-1", timestamp=_AS_OF
    )

    assert entry.invocation_id == "inv-1"
    assert entry.timestamp == _AS_OF
    assert entry.event_type is EventType.DISTILLATION_ANOMALY_FLAG
    assert entry.event_group is EventGroup.DISTILLATION_ANOMALY
    assert entry.source is EventSource.DISTILLATION_ORCHESTRATOR
    assert entry.position_id is None
    assert entry.order_id is None
    assert entry.thesis_id is None

    detail = entry.detail
    assert isinstance(detail, DistillationAnomalyFlagDetail)
    # resolve_flag_taxonomy("volume_anomaly") -> ("anomaly_detection", "volume_anomaly_sigma")
    assert detail.threshold_class == "anomaly_detection"
    assert detail.threshold_key == "volume_anomaly_sigma"
    assert detail.magnitude == 4.2
    assert detail.severity == "investigate_now"
    assert detail.calibration_state is CalibrationState.CALIBRATED
    assert detail.block_id == "q1.volume.NVDA"
    # Market-wide flag (no embedded ticker) -> None.
    assert detail.ticker is None


def test_mapper_extracts_ticker_from_ticker_bearing_flag() -> None:
    """``correlation_locus_flag:{ticker}`` carries the ticker into the detail."""
    summary = _summary(name="correlation_locus_flag:NVDA", block_id="q7.correlation_locus.NVDA")

    detail = anomaly_summary_to_activity_log_entry(
        summary, invocation_id="inv-1", timestamp=_AS_OF
    ).detail
    assert isinstance(detail, DistillationAnomalyFlagDetail)
    assert detail.ticker == "NVDA"
    # The dynamic suffix is stripped before taxonomy resolution.
    assert detail.threshold_class == "narrative_lag"
    assert detail.threshold_key == "correlation_locus_pair_count_threshold"


def test_mapper_pair_key_flag_has_no_ticker() -> None:
    """A pair / pair-key flag (two-segment or pair-key suffix) resolves ticker to ``None``.

    ``correlation_breakdown_flag:{row}:{col}`` is a pair; ``overdue_lag_flag:{pair_key}``
    is a pair-key. Neither names a single subject ticker, so the detail's ``ticker``
    is ``None`` even though a ``:``-suffix is present.
    """
    pair = _summary(name="correlation_breakdown_flag:NVDA:AMD", block_id="q7.cbd.NVDA_AMD")
    pair_key = _summary(name="overdue_lag_flag:SOXX_QQQ", block_id="q7.lead_lag.SOXX_QQQ")

    for summary in (pair, pair_key):
        detail = anomaly_summary_to_activity_log_entry(
            summary, invocation_id="inv-1", timestamp=_AS_OF
        ).detail
        assert isinstance(detail, DistillationAnomalyFlagDetail)
        assert detail.ticker is None
