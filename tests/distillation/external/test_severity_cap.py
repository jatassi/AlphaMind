"""Tests for the calibration-state severity cap — ALP-544.

The publishing-layer transform caps anomaly-flag severity by the source
block's calibration state:

- ``calibrated`` — severity passes through unchanged.
- ``accumulating`` — severity capped at ``investigate_if_persists``.
- ``unavailable`` — severity capped at ``note_for_context``.

A configurable exempt-flag-names set lets structural signals (e.g.,
macro_surprise_anomaly) bypass the cap when they would legitimately fire
at full severity even with limited history.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._severity_cap import (
    cap_anomaly_severity,
    cap_block_severities,
    cap_blocks_for_calibration,
)
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)

_FRESHNESS = datetime(2026, 5, 18, 11, 11, 40, tzinfo=UTC)


def _make_block(
    *,
    state: CalibrationState,
    flags: tuple[AnomalyFlag, ...] = (),
    bootstrap_reason: str | None = None,
    block_id: str = "q6.funding_stress",
) -> OutputBlock:
    return OutputBlock(
        block_id=block_id,
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=_FRESHNESS,
        calibration_state=state,
        bootstrap_reason=bootstrap_reason,
        payload={},
        anomaly_flags=flags,
        regime_context=None,
    )


def test_cap_unchanged_when_calibrated() -> None:
    """A calibrated block's flag passes through unchanged."""
    assert (
        cap_anomaly_severity(
            severity="investigate_now",
            state=CalibrationState.CALIBRATED,
            exempt=False,
        )
        == "investigate_now"
    )


def test_cap_to_investigate_if_persists_when_accumulating() -> None:
    """Accumulating downgrades investigate_now → investigate_if_persists."""
    assert (
        cap_anomaly_severity(
            severity="investigate_now",
            state=CalibrationState.ACCUMULATING,
            exempt=False,
        )
        == "investigate_if_persists"
    )


def test_cap_to_note_for_context_when_unavailable() -> None:
    """Unavailable downgrades all severities to note_for_context."""
    assert (
        cap_anomaly_severity(
            severity="investigate_now",
            state=CalibrationState.UNAVAILABLE,
            exempt=False,
        )
        == "note_for_context"
    )


def test_cap_does_not_elevate_below_cap_severity() -> None:
    """An already-low severity isn't raised by the cap."""
    assert (
        cap_anomaly_severity(
            severity="note_for_context",
            state=CalibrationState.ACCUMULATING,
            exempt=False,
        )
        == "note_for_context"
    )


def test_cap_skipped_for_exempt_flag() -> None:
    """An exempt flag retains its producer-side severity regardless of state."""
    assert (
        cap_anomaly_severity(
            severity="investigate_now",
            state=CalibrationState.ACCUMULATING,
            exempt=True,
        )
        == "investigate_now"
    )


def test_cap_block_preserves_magnitude_and_name() -> None:
    """Only severity is rewritten; magnitude and name carry through unchanged."""
    flag = AnomalyFlag(name="funding_stress_alert", magnitude=4.00, severity="investigate_now")
    block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=(flag,),
        bootstrap_reason="funding_stress_min_observations: 11 < 60",
    )

    capped = cap_block_severities(block, exempt_flag_names=frozenset())

    assert len(capped.anomaly_flags) == 1
    capped_flag = capped.anomaly_flags[0]
    assert capped_flag.name == "funding_stress_alert"
    assert capped_flag.magnitude == 4.00
    assert capped_flag.severity == "investigate_if_persists"


def test_cap_block_returns_same_block_when_calibrated() -> None:
    """A calibrated block with flags is returned unchanged (identity)."""
    flag = AnomalyFlag(name="x", magnitude=1.0, severity="investigate_now")
    block = _make_block(state=CalibrationState.CALIBRATED, flags=(flag,))

    capped = cap_block_severities(block, exempt_flag_names=frozenset())

    assert capped is block


def test_cap_block_returns_same_block_when_no_flags() -> None:
    """A block with no flags is returned unchanged regardless of state."""
    block = _make_block(state=CalibrationState.ACCUMULATING)

    capped = cap_block_severities(block, exempt_flag_names=frozenset())

    assert capped is block


def test_cap_blocks_skips_exempt_flags_by_name() -> None:
    """Flags named in the exempt set keep their producer severity under non-calibrated."""
    exempt_flag = AnomalyFlag(
        name="macro_surprise_anomaly", magnitude=3.5, severity="investigate_now"
    )
    capped_flag = AnomalyFlag(
        name="funding_stress_alert", magnitude=4.0, severity="investigate_now"
    )
    block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=(exempt_flag, capped_flag),
    )

    out = cap_blocks_for_calibration(
        [block],
        exempt_flag_names=frozenset({"macro_surprise_anomaly"}),
    )

    assert len(out) == 1
    new_flags = out[0].anomaly_flags
    by_name = {f.name: f for f in new_flags}
    assert by_name["macro_surprise_anomaly"].severity == "investigate_now"
    assert by_name["funding_stress_alert"].severity == "investigate_if_persists"


def test_cap_blocks_caps_uniformly_across_all_emitting_modules() -> None:
    """The cap is uniform: every block with non-calibrated state has its flags capped."""
    accumulating_block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=(
            AnomalyFlag(name="funding_stress_alert", magnitude=4.0, severity="investigate_now"),
        ),
        block_id="q6.funding_stress",
    )
    unavailable_block = _make_block(
        state=CalibrationState.UNAVAILABLE,
        flags=(
            AnomalyFlag(
                name="correlation_breakdown_flag:GOOG:META",
                magnitude=5.83,
                severity="investigate_now",
            ),
        ),
        block_id="q7.correlation_breakdown.GOOG_META",
    )
    calibrated_block = _make_block(
        state=CalibrationState.CALIBRATED,
        flags=(AnomalyFlag(name="narrative_lag_flag", magnitude=5.83, severity="investigate_now"),),
        block_id="q7.narrative_lag",
    )

    out = cap_blocks_for_calibration(
        [accumulating_block, unavailable_block, calibrated_block],
        exempt_flag_names=frozenset(),
    )

    severities = {b.block_id: b.anomaly_flags[0].severity for b in out}
    assert severities["q6.funding_stress"] == "investigate_if_persists"
    assert severities["q7.correlation_breakdown.GOOG_META"] == "note_for_context"
    assert severities["q7.narrative_lag"] == "investigate_now"
