"""Tests for the emergency-invocation header (story 06)."""

from __future__ import annotations

import pytest

from alphamind.risk_guardrails import breach_behavior as breach_behavior_pkg
from alphamind.risk_guardrails.breach_behavior import (
    EmergencyContext,
    EmergencyTrigger,
)
from alphamind.risk_guardrails.state_delivery.emergency import (
    prepend_emergency_block,
    render_emergency_block,
)


def test_emergency_trigger_is_canonical_breach_behavior_class() -> None:
    """The state-delivery import must be the same class identity as breach-behavior."""
    assert EmergencyTrigger is breach_behavior_pkg.EmergencyTrigger


def test_emergency_context_is_canonical_breach_behavior_class() -> None:
    """The state-delivery import must be the same class identity as breach-behavior."""
    assert EmergencyContext is breach_behavior_pkg.EmergencyContext


def test_state_delivery_package_reexports_emergency_surface() -> None:
    """The state-delivery package re-exports the emergency renderers and canonical types."""
    import alphamind.risk_guardrails.state_delivery as state_delivery

    assert state_delivery.render_emergency_block is render_emergency_block
    assert state_delivery.prepend_emergency_block is prepend_emergency_block
    assert state_delivery.EmergencyContext is EmergencyContext
    assert state_delivery.EmergencyTrigger is EmergencyTrigger
    for name in (
        "render_emergency_block",
        "prepend_emergency_block",
        "EmergencyContext",
        "EmergencyTrigger",
    ):
        assert name in state_delivery.__all__


def test_emergency_trigger_has_all_four_documented_values() -> None:
    """Per breach-behavior.md, four trigger types exist; each maps to a non-empty string."""
    expected = {
        "REGIME_JUMP": "regime_jump",
        "MULTI_RULE_BREACH": "multi_rule_breach",
        "DAILY_DRAWDOWN_VELOCITY": "daily_drawdown_velocity",
        "MARGIN_CALL": "margin_call",
    }
    for member_name, value in expected.items():
        member = EmergencyTrigger[member_name]
        assert member.value == value
        assert isinstance(member.value, str)
        assert member.value


def test_emergency_context_rejects_non_positive_minutes_since_last_invocation() -> None:
    with pytest.raises(ValueError, match="positive"):
        EmergencyContext(
            trigger=EmergencyTrigger.REGIME_JUMP,
            trigger_detail="x",
            minutes_since_last_invocation=0.0,
            normal_cadence_minutes=120.0,
        )


def test_emergency_context_rejects_non_positive_normal_cadence_minutes() -> None:
    with pytest.raises(ValueError, match="positive"):
        EmergencyContext(
            trigger=EmergencyTrigger.REGIME_JUMP,
            trigger_detail="x",
            minutes_since_last_invocation=27.0,
            normal_cadence_minutes=-1.0,
        )


def test_render_emergency_block_regime_jump_matches_design() -> None:
    """The rendered block matches the design's worked example verbatim."""
    context = EmergencyContext(
        trigger=EmergencyTrigger.REGIME_JUMP,
        trigger_detail="Regime jump: low-vol → crisis (VIX 12 → 38)",
        minutes_since_last_invocation=27.0,
        normal_cadence_minutes=120.0,
    )
    expected = (
        "** EMERGENCY INVOCATION — trigger: regime_jump **\n"
        "Trigger detail: Regime jump: low-vol → crisis (VIX 12 → 38)\n"
        "Time since last invocation: 27m (normal cadence: ~120m)"
    )
    assert render_emergency_block(context) == expected


@pytest.mark.parametrize("trigger", list(EmergencyTrigger))
def test_render_emergency_block_emits_three_lines_for_every_trigger(
    trigger: EmergencyTrigger,
) -> None:
    context = EmergencyContext(
        trigger=trigger,
        trigger_detail="some detail",
        minutes_since_last_invocation=15.0,
        normal_cadence_minutes=60.0,
    )
    rendered = render_emergency_block(context)
    lines = rendered.split("\n")
    assert len(lines) == 3
    assert lines[0] == f"** EMERGENCY INVOCATION — trigger: {trigger.value} **"
    assert lines[1] == "Trigger detail: some detail"
    assert lines[2] == "Time since last invocation: 15m (normal cadence: ~60m)"


def test_render_emergency_block_rounds_fractional_minutes_to_nearest_int() -> None:
    """Per the spec, ``27.4 → 27m`` and ``120.0 → 120m`` (no decimals on whole values)."""
    context = EmergencyContext(
        trigger=EmergencyTrigger.MARGIN_CALL,
        trigger_detail="x",
        minutes_since_last_invocation=27.4,
        normal_cadence_minutes=120.0,
    )
    rendered = render_emergency_block(context)
    assert "Time since last invocation: 27m (normal cadence: ~120m)" in rendered


def test_render_emergency_block_rounds_fractional_minutes_up_when_appropriate() -> None:
    """``27.6`` rounds to ``28m`` (bankers' rounding for ``.5`` is acceptable)."""
    context = EmergencyContext(
        trigger=EmergencyTrigger.MARGIN_CALL,
        trigger_detail="x",
        minutes_since_last_invocation=27.6,
        normal_cadence_minutes=120.0,
    )
    rendered = render_emergency_block(context)
    assert "Time since last invocation: 28m (normal cadence: ~120m)" in rendered


# ---------------------------------------------------------------------------
# prepend_emergency_block
# ---------------------------------------------------------------------------


_ENVELOPE_OPEN_LINE = "=== GUARDRAIL STATE (invocation INV-001, 2026-04-28T12:00:00Z) ==="

_STUB_HEADER = "\n".join(
    [
        _ENVELOPE_OPEN_LINE,
        "Regime: normal [unchanged]",
        "",
        "Capital:",
        "  Available for new positions: $1,000,000 (10.0% of portfolio)",
        "===",
    ]
)


def _make_context() -> EmergencyContext:
    return EmergencyContext(
        trigger=EmergencyTrigger.REGIME_JUMP,
        trigger_detail="Regime jump: low-vol → crisis (VIX 12 → 38)",
        minutes_since_last_invocation=27.0,
        normal_cadence_minutes=120.0,
    )


def test_prepend_emergency_block_inserts_after_envelope_open_line() -> None:
    composed = prepend_emergency_block(header=_STUB_HEADER, context=_make_context())
    lines = composed.split("\n")
    assert lines[0] == _ENVELOPE_OPEN_LINE
    assert lines[1] == "** EMERGENCY INVOCATION — trigger: regime_jump **"
    assert lines[2] == "Trigger detail: Regime jump: low-vol → crisis (VIX 12 → 38)"
    assert lines[3] == "Time since last invocation: 27m (normal cadence: ~120m)"
    # Blank line separates the emergency block from the next block.
    assert lines[4] == ""
    # The original next line follows.
    assert lines[5] == "Regime: normal [unchanged]"


def test_prepend_emergency_block_raises_when_envelope_open_missing() -> None:
    bad_header = "Regime: normal [unchanged]\nCapital:\n==="
    with pytest.raises(ValueError, match="envelope-open"):
        prepend_emergency_block(header=bad_header, context=_make_context())


def test_prepend_emergency_block_raises_when_envelope_open_duplicated() -> None:
    duplicated = "\n".join(
        [
            _ENVELOPE_OPEN_LINE,
            "Regime: normal [unchanged]",
            _ENVELOPE_OPEN_LINE,
            "===",
        ]
    )
    with pytest.raises(ValueError, match="multiple envelope-open"):
        prepend_emergency_block(header=duplicated, context=_make_context())


def test_prepend_emergency_block_is_deterministic() -> None:
    context = _make_context()
    first = prepend_emergency_block(header=_STUB_HEADER, context=context)
    second = prepend_emergency_block(header=_STUB_HEADER, context=context)
    assert first == second


def test_prepend_emergency_block_introduces_no_double_blank_lines() -> None:
    """The wrapper inserts exactly one blank-line separator after the emergency block."""
    composed = prepend_emergency_block(header=_STUB_HEADER, context=_make_context())
    assert "\n\n\n" not in composed


def test_prepend_emergency_block_does_not_introduce_trailing_blank_line() -> None:
    composed = prepend_emergency_block(header=_STUB_HEADER, context=_make_context())
    # The original header had no trailing blank; the wrapper must not add one.
    assert not composed.endswith("\n")


def test_prepend_emergency_block_co_occurs_with_halt_mode_wrapped_header() -> None:
    """Per the design, the emergency block lands before the halt-mode banner.

    Halt-mode restrictions still take behavioral precedence; both blocks render.
    """
    halt_wrapped_header = "\n".join(
        [
            _ENVELOPE_OPEN_LINE,
            "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **",
            "Mode: WATCHLIST ONLY — do not generate trade proposals",
            "",
            "Regime: crisis [CHANGED since last invocation]",
            "===",
        ]
    )
    composed = prepend_emergency_block(header=halt_wrapped_header, context=_make_context())
    lines = composed.split("\n")
    assert lines[0] == _ENVELOPE_OPEN_LINE
    assert lines[1] == "** EMERGENCY INVOCATION — trigger: regime_jump **"
    assert lines[2] == "Trigger detail: Regime jump: low-vol → crisis (VIX 12 → 38)"
    assert lines[3] == "Time since last invocation: 27m (normal cadence: ~120m)"
    assert lines[4] == ""
    assert lines[5] == "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **"
    assert lines[6] == "Mode: WATCHLIST ONLY — do not generate trade proposals"
