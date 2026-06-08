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

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

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
    payload: Mapping[str, Any] | None = None,
) -> OutputBlock:
    return OutputBlock(
        block_id=block_id,
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        freshness_ts=_FRESHNESS,
        calibration_state=state,
        bootstrap_reason=bootstrap_reason,
        payload=payload if payload is not None else {},
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


# ---------------------------------------------------------------------------
# ALP-627 — per-ticker payload severity cap.
#
# q1 anomaly producers bake the producer-side severity into
# ``payload["per_ticker"][ticker]["severity"]`` so the per-ticker renderer
# can surface it inline. The cap previously only rewrote
# ``anomaly_flags`` and left the payload rows uncapped, surfacing two
# contradictory severities for the same flag. These tests pin the cap's
# extended contract: per-ticker payload severity matches the post-cap
# flag severity for every q1 anomaly block.
# ---------------------------------------------------------------------------


def _q1_volume_block(*, state: CalibrationState) -> OutputBlock:
    """Build a synthetic q1.volume_anomaly block in the producer's shape."""
    flag = AnomalyFlag(name="volume_anomaly", magnitude=2.64, severity="investigate_now")
    payload = {
        "per_ticker": {
            "MSFT": {
                "today_volume": 5.081e7,
                "baseline_mean": 3.333e7,
                "baseline_stdev": 6.61e6,
                "deviation_sigma": 2.645,
                "severity": "investigate_now",
            },
        },
    }
    return _make_block(
        state=state,
        flags=(flag,),
        bootstrap_reason=(
            None if state is CalibrationState.CALIBRATED else "volume_baseline_days: 11 < 20"
        ),
        block_id="q1.volume_anomaly",
        payload=payload,
    )


def _q1_price_move_block(*, state: CalibrationState) -> OutputBlock:
    """Build a synthetic q1.price_move_anomaly block in the producer's shape."""
    flag = AnomalyFlag(name="price_move_anomaly", magnitude=2.45, severity="investigate_now")
    payload = {
        "per_ticker": {
            "CTRA": {
                "atr": 1.254,
                "atr_multiple": 2.448,
                "price_move": -3.07,
                "severity": "investigate_now",
            },
        },
    }
    return _make_block(
        state=state,
        flags=(flag,),
        bootstrap_reason=(
            None if state is CalibrationState.CALIBRATED else "atr_baseline_days: 4 < 20"
        ),
        block_id="q1.price_move_anomaly",
        payload=payload,
    )


def test_q1_volume_per_ticker_severity_unchanged_when_calibrated() -> None:
    """Calibrated q1.volume_anomaly: per-ticker severity stays at producer value."""
    block = _q1_volume_block(state=CalibrationState.CALIBRATED)
    capped = cap_block_severities(block, exempt_flag_names=frozenset())
    assert capped.anomaly_flags[0].severity == "investigate_now"
    assert capped.payload["per_ticker"]["MSFT"]["severity"] == "investigate_now"


def test_q1_volume_per_ticker_severity_capped_when_accumulating() -> None:
    """Accumulating q1.volume_anomaly: per-ticker severity == capped flag severity."""
    block = _q1_volume_block(state=CalibrationState.ACCUMULATING)
    capped = cap_block_severities(block, exempt_flag_names=frozenset())
    assert capped.anomaly_flags[0].severity == "investigate_if_persists"
    assert capped.payload["per_ticker"]["MSFT"]["severity"] == "investigate_if_persists"


def test_q1_volume_per_ticker_severity_capped_when_unavailable() -> None:
    """Unavailable q1.volume_anomaly: both views downgrade to note_for_context."""
    block = _q1_volume_block(state=CalibrationState.UNAVAILABLE)
    capped = cap_block_severities(block, exempt_flag_names=frozenset())
    assert capped.anomaly_flags[0].severity == "note_for_context"
    assert capped.payload["per_ticker"]["MSFT"]["severity"] == "note_for_context"


def test_q1_price_move_per_ticker_severity_unchanged_when_calibrated() -> None:
    """Calibrated q1.price_move_anomaly: per-ticker severity stays at producer value."""
    block = _q1_price_move_block(state=CalibrationState.CALIBRATED)
    capped = cap_block_severities(block, exempt_flag_names=frozenset())
    assert capped.anomaly_flags[0].severity == "investigate_now"
    assert capped.payload["per_ticker"]["CTRA"]["severity"] == "investigate_now"


def test_q1_price_move_per_ticker_severity_capped_when_accumulating() -> None:
    """Accumulating q1.price_move_anomaly: per-ticker severity == capped flag severity."""
    block = _q1_price_move_block(state=CalibrationState.ACCUMULATING)
    capped = cap_block_severities(block, exempt_flag_names=frozenset())
    assert capped.anomaly_flags[0].severity == "investigate_if_persists"
    assert capped.payload["per_ticker"]["CTRA"]["severity"] == "investigate_if_persists"


def test_q1_price_move_per_ticker_severity_capped_when_unavailable() -> None:
    """Unavailable q1.price_move_anomaly: both views downgrade to note_for_context."""
    block = _q1_price_move_block(state=CalibrationState.UNAVAILABLE)
    capped = cap_block_severities(block, exempt_flag_names=frozenset())
    assert capped.anomaly_flags[0].severity == "note_for_context"
    assert capped.payload["per_ticker"]["CTRA"]["severity"] == "note_for_context"


def test_q1_per_ticker_preserves_magnitude_fields_under_cap() -> None:
    """Only the per-ticker severity is rewritten; magnitude fields carry through."""
    block = _q1_volume_block(state=CalibrationState.ACCUMULATING)
    capped = cap_block_severities(block, exempt_flag_names=frozenset())
    row = capped.payload["per_ticker"]["MSFT"]
    assert row["today_volume"] == 5.081e7
    assert row["baseline_mean"] == 3.333e7
    assert row["baseline_stdev"] == 6.61e6
    assert row["deviation_sigma"] == 2.645


def test_q1_per_ticker_severity_skipped_when_flag_exempt() -> None:
    """Exempt flag name: per-ticker severity stays at producer value alongside the flag."""
    flag = AnomalyFlag(name="volume_anomaly", magnitude=2.64, severity="investigate_now")
    payload = {
        "per_ticker": {
            "MSFT": {
                "deviation_sigma": 2.645,
                "severity": "investigate_now",
            },
        },
    }
    block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=(flag,),
        bootstrap_reason="volume_baseline_days: 11 < 20",
        block_id="q1.volume_anomaly",
        payload=payload,
    )

    capped = cap_block_severities(block, exempt_flag_names=frozenset({"volume_anomaly"}))

    assert capped.anomaly_flags[0].severity == "investigate_now"
    assert capped.payload["per_ticker"]["MSFT"]["severity"] == "investigate_now"


def test_q1_per_ticker_multi_ticker_block_caps_every_row() -> None:
    """A multi-ticker q1 block caps every per-ticker row in one pass.

    Since ALP-934 the per-ticker flags carry distinct ``:{ticker}``-suffixed
    names (so each gets its own ``activity_log`` entry_id); they share the block's
    one calibration state, so they still cap to a single common severity and the
    per-ticker payload-severity rewrite stays unambiguous.
    """
    flags = (
        AnomalyFlag(name="volume_anomaly:MSFT", magnitude=2.64, severity="investigate_now"),
        AnomalyFlag(name="volume_anomaly:NVDA", magnitude=3.10, severity="investigate_now"),
    )
    payload = {
        "per_ticker": {
            "MSFT": {"deviation_sigma": 2.645, "severity": "investigate_now"},
            "NVDA": {"deviation_sigma": 3.10, "severity": "investigate_now"},
        },
    }
    block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=flags,
        bootstrap_reason="volume_baseline_days: 11 < 20",
        block_id="q1.volume_anomaly",
        payload=payload,
    )

    capped = cap_block_severities(block, exempt_flag_names=frozenset())

    assert all(f.severity == "investigate_if_persists" for f in capped.anomaly_flags)
    rows = capped.payload["per_ticker"]
    assert rows["MSFT"]["severity"] == "investigate_if_persists"
    assert rows["NVDA"]["severity"] == "investigate_if_persists"


def test_cap_preserves_block_when_payload_severity_already_matches_capped_value() -> None:
    """Accumulating block whose per-ticker severity already equals the capped value: identity."""
    flag = AnomalyFlag(name="volume_anomaly", magnitude=2.64, severity="investigate_if_persists")
    payload = {
        "per_ticker": {
            "MSFT": {
                "deviation_sigma": 2.645,
                "severity": "investigate_if_persists",
            },
        },
    }
    block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=(flag,),
        bootstrap_reason="volume_baseline_days: 11 < 20",
        block_id="q1.volume_anomaly",
        payload=payload,
    )

    capped = cap_block_severities(block, exempt_flag_names=frozenset())

    assert capped is block


def test_cap_rejects_multi_severity_block_with_per_ticker_severity() -> None:
    """Flags capping to >1 distinct severity + per-ticker severity is rejected.

    The per-ticker row has no flag-name field, so the cap has no way to pick the
    right post-cap severity when the block's flags resolve to *different* capped
    severities. (Since ALP-934 a q1 anomaly block legitimately carries multiple
    distinct flag *names* — one per ticker — but they share one calibration state
    and so cap to one common severity; the breach is divergent *severities*, not
    divergent names.) The silent fall-through would regress to the ALP-627 bug;
    the assertion surfaces the contract breach at first emission. Here the two
    flags cap to ``investigate_if_persists`` and ``note_for_context``.
    """
    flags = (
        AnomalyFlag(name="volume_anomaly:MSFT", magnitude=2.64, severity="investigate_now"),
        AnomalyFlag(name="some_other_flag", magnitude=1.0, severity="note_for_context"),
    )
    payload = {
        "per_ticker": {
            "MSFT": {"deviation_sigma": 2.645, "severity": "investigate_now"},
        },
    }
    block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=flags,
        bootstrap_reason="volume_baseline_days: 11 < 20",
        block_id="q1.volume_anomaly",
        payload=payload,
    )

    with pytest.raises(AssertionError, match="single capped severity"):
        cap_block_severities(block, exempt_flag_names=frozenset())


def test_cap_allows_multi_flag_name_block_without_per_ticker_severity() -> None:
    """Multi-distinct-flag-name is fine when no per-ticker row carries severity."""
    flags = (
        AnomalyFlag(name="volume_anomaly", magnitude=2.64, severity="investigate_now"),
        AnomalyFlag(name="some_other_flag", magnitude=1.0, severity="note_for_context"),
    )
    payload = {"per_ticker": {"MSFT": {"deviation_sigma": 2.645}}}
    block = _make_block(
        state=CalibrationState.ACCUMULATING,
        flags=flags,
        bootstrap_reason="volume_baseline_days: 11 < 20",
        block_id="hypothetical.multi_flag_block",
        payload=payload,
    )

    capped = cap_block_severities(block, exempt_flag_names=frozenset())

    # Flag severities still cap individually; payload is left untouched.
    severities = {f.name: f.severity for f in capped.anomaly_flags}
    assert severities["volume_anomaly"] == "investigate_if_persists"
    assert severities["some_other_flag"] == "note_for_context"
    assert capped.payload["per_ticker"]["MSFT"] == {"deviation_sigma": 2.645}
