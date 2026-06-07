"""Distillation-anomaly activity_log contract tests (ALP-877).

Verifies:
- EventType, EventGroup, EventSource vocabulary
- DistillationAnomalyFlagDetail field shape + validation
- Codec round-trip (encode_detail → decode_detail)
- Mapping resolution via EVENT_TYPE_TO_DETAIL_CLASS / EVENT_TYPE_TO_GROUP
- Metadata-created schema admits an anomaly-typed activity_log row
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from alphamind._kernel.calibration import CalibrationState
from alphamind.persistence.models import Base
from alphamind.portfolio_state.events import (
    EVENT_TYPE_TO_DETAIL_CLASS,
    EVENT_TYPE_TO_GROUP,
    AnyDetailType,
    EventGroup,
    EventSource,
    EventType,
    decode_detail,
    encode_detail,
)
from alphamind.portfolio_state.events.distillation_anomaly import (
    DistillationAnomalyFlagDetail,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_detail(**overrides: object) -> DistillationAnomalyFlagDetail:
    """Return a minimal valid DistillationAnomalyFlagDetail."""
    defaults: dict[str, object] = {
        "threshold_class": "volume",
        "threshold_key": "volume_anomaly_sigma",
        "magnitude": 3.5,
        "severity": "investigate_now",
        "ticker": "NVDA",
        "calibration_state": CalibrationState.CALIBRATED,
        "block_id": "q1.volume",
    }
    defaults.update(overrides)
    return DistillationAnomalyFlagDetail(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cycle 1: Vocabulary
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_distillation_anomaly_group_exists(self) -> None:
        assert EventGroup.DISTILLATION_ANOMALY == "DISTILLATION_ANOMALY"

    def test_distillation_anomaly_flag_event_type_exists(self) -> None:
        assert EventType.DISTILLATION_ANOMALY_FLAG == "DISTILLATION_ANOMALY_FLAG"

    def test_distillation_orchestrator_event_source_exists(self) -> None:
        assert EventSource.DISTILLATION_ORCHESTRATOR == "DISTILLATION_ORCHESTRATOR"


# ---------------------------------------------------------------------------
# Cycle 2: Detail payload shape
# ---------------------------------------------------------------------------


class TestDetailPayloadShape:
    def test_all_required_fields_present(self) -> None:
        detail = _minimal_detail()
        assert detail.threshold_class == "volume"
        assert detail.threshold_key == "volume_anomaly_sigma"
        assert detail.magnitude == 3.5
        assert detail.severity == "investigate_now"
        assert detail.ticker == "NVDA"
        assert detail.calibration_state is CalibrationState.CALIBRATED
        assert detail.block_id == "q1.volume"

    def test_ticker_may_be_none(self) -> None:
        detail = _minimal_detail(ticker=None)
        assert detail.ticker is None

    def test_detail_is_frozen(self) -> None:
        detail = _minimal_detail()
        with pytest.raises((AttributeError, TypeError)):
            detail.magnitude = 1.0  # type: ignore[misc]

    def test_invalid_severity_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _minimal_detail(severity="not_a_real_severity")

    def test_empty_threshold_class_raises(self) -> None:
        with pytest.raises(ValueError):
            _minimal_detail(threshold_class="")

    def test_empty_threshold_key_raises(self) -> None:
        with pytest.raises(ValueError):
            _minimal_detail(threshold_key="")

    def test_empty_block_id_raises(self) -> None:
        with pytest.raises(ValueError):
            _minimal_detail(block_id="")

    def test_all_valid_severities_accepted(self) -> None:
        for severity in ("investigate_now", "investigate_if_persists", "note_for_context"):
            d = _minimal_detail(severity=severity)
            assert d.severity == severity


# ---------------------------------------------------------------------------
# Cycle 3: Codec round-trip
# ---------------------------------------------------------------------------


class TestCodecRoundTrip:
    def test_encode_then_decode_is_identity(self) -> None:
        detail = _minimal_detail()
        encoded = encode_detail(detail)
        decoded = decode_detail(encoded, DistillationAnomalyFlagDetail)
        assert decoded == detail

    def test_encode_produces_valid_json(self) -> None:
        detail = _minimal_detail()
        encoded = encode_detail(detail)
        parsed = json.loads(encoded)
        assert parsed["threshold_class"] == "volume"
        assert parsed["threshold_key"] == "volume_anomaly_sigma"
        assert parsed["magnitude"] == 3.5
        assert parsed["severity"] == "investigate_now"
        assert parsed["ticker"] == "NVDA"
        assert parsed["calibration_state"] == "calibrated"
        assert parsed["block_id"] == "q1.volume"

    def test_round_trip_with_none_ticker(self) -> None:
        detail = _minimal_detail(ticker=None)
        encoded = encode_detail(detail)
        decoded = decode_detail(encoded, DistillationAnomalyFlagDetail)
        assert decoded == detail
        assert decoded.ticker is None

    def test_round_trip_all_severities(self) -> None:
        for severity in ("investigate_now", "investigate_if_persists", "note_for_context"):
            detail = _minimal_detail(severity=severity)
            decoded = decode_detail(encode_detail(detail), DistillationAnomalyFlagDetail)
            assert decoded == detail

    def test_round_trip_all_calibration_states(self) -> None:
        for state in CalibrationState:
            detail = _minimal_detail(calibration_state=state)
            decoded = decode_detail(encode_detail(detail), DistillationAnomalyFlagDetail)
            assert decoded == detail


# ---------------------------------------------------------------------------
# Cycle 4: Mapping resolution
# ---------------------------------------------------------------------------


class TestMappingResolution:
    def test_event_type_resolves_to_correct_detail_class(self) -> None:
        assert (
            EVENT_TYPE_TO_DETAIL_CLASS[EventType.DISTILLATION_ANOMALY_FLAG]
            is DistillationAnomalyFlagDetail
        )

    def test_event_type_resolves_to_correct_group(self) -> None:
        assert (
            EVENT_TYPE_TO_GROUP[EventType.DISTILLATION_ANOMALY_FLAG]
            is EventGroup.DISTILLATION_ANOMALY
        )

    def test_all_event_types_covered(self) -> None:
        """Exhaustiveness: every EventType member has an entry in both dicts."""
        assert set(EVENT_TYPE_TO_DETAIL_CLASS) == set(EventType)
        assert set(EVENT_TYPE_TO_GROUP) == set(EventType)

    def test_detail_class_is_member_of_any_detail_type(self) -> None:
        """DistillationAnomalyFlagDetail is in the AnyDetailType union.

        AnyDetailType is a runtime union expression; Python 3.10+
        supports isinstance(x, X | Y) on union-typed expressions.
        """
        detail = _minimal_detail()
        assert isinstance(detail, AnyDetailType)


# ---------------------------------------------------------------------------
# Cycle 5: Schema admission (metadata-created)
# ---------------------------------------------------------------------------


class TestSchemaAdmission:
    """The metadata-created activity_log CHECK admits the new event type.

    SQLite does not enforce FK constraints without ``PRAGMA foreign_keys=ON``.
    We deliberately leave FK enforcement off so we can test just the CHECK
    constraint (which IS enforced) without seeding the full invocations /
    process_lifetimes chain.
    """

    def _make_engine(self) -> sa.Engine:
        # Import state.tables so all FK targets are registered with Base.metadata
        import alphamind.state.tables  # noqa: F401

        engine = sa.create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return engine

    def test_anomaly_typed_row_is_accepted(self) -> None:
        engine = self._make_engine()
        with Session(engine) as session:
            _cols = (
                "entry_id, invocation_id, entry_at, event_type, event_group, source, detail_json"
            )
            session.execute(
                sa.text(
                    f"INSERT INTO activity_log ({_cols}) "
                    "VALUES (:eid, :iid, :at, :et, :eg, :src, :dj)"
                ),
                {
                    "eid": "entry-001",
                    "iid": "inv-001",
                    "at": "2026-01-01T00:00:00Z",
                    "et": EventType.DISTILLATION_ANOMALY_FLAG.value,
                    "eg": EventGroup.DISTILLATION_ANOMALY.value,
                    "src": EventSource.DISTILLATION_ORCHESTRATOR.value,
                    "dj": encode_detail(_minimal_detail()),
                },
            )
            session.commit()

        with Session(engine) as session:
            row = session.execute(
                sa.text("SELECT event_type, event_group, source FROM activity_log")
            ).one()
            assert row.event_type == "DISTILLATION_ANOMALY_FLAG"
            assert row.event_group == "DISTILLATION_ANOMALY"
            assert row.source == "DISTILLATION_ORCHESTRATOR"

    def test_unknown_event_type_is_rejected_by_check(self) -> None:
        """The CHECK constraint rejects an unrecognized event_type value."""
        engine = self._make_engine()
        _cols = "entry_id, invocation_id, entry_at, event_type, event_group, source, detail_json"
        with Session(engine) as session, pytest.raises(Exception):  # noqa: B017
            session.execute(
                sa.text(
                    f"INSERT INTO activity_log ({_cols}) "
                    "VALUES (:eid, :iid, :at, :et, :eg, :src, :dj)"
                ),
                {
                    "eid": "entry-bad",
                    "iid": "inv-001",
                    "at": "2026-01-01T00:00:00Z",
                    "et": "NOT_A_REAL_EVENT_TYPE",
                    "eg": EventGroup.DISTILLATION_ANOMALY.value,
                    "src": EventSource.DISTILLATION_ORCHESTRATOR.value,
                    "dj": "{}",
                },
            )
            session.commit()
