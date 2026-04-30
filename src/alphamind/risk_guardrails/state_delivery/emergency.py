"""Emergency-invocation header block (story 06).

Renders the four-line emergency block documented in
``docs/design/06-risk-guardrails/state-delivery.md`` § *Emergency invocation
header* and prepends it to a fully-rendered audience header (analyst,
strategist, PM — normal or halt-mode wrapped).

The wrapper is intentionally a string-level operation: audience renderers
return assembled strings, so treating their output as opaque keeps the
dependency direction one-way. ``EmergencyTrigger`` and ``EmergencyContext``
are owned by the breach-behavior package and imported unchanged.
"""

from __future__ import annotations

import re

from alphamind.risk_guardrails.breach_behavior import EmergencyContext

_ENVELOPE_OPEN_PATTERN = re.compile(r"^=== GUARDRAIL STATE \(invocation [^)]+, [^)]+\) ===$")


def render_emergency_block(context: EmergencyContext) -> str:
    """Render the three-line emergency invocation block.

    The block carries the trigger header, the upstream-supplied trigger
    detail, and the elapsed time since the last invocation alongside the
    scheduler's normal cadence. Minute values render with whole-minute
    precision per the design's worked example.
    """
    minutes = round(context.minutes_since_last_invocation)
    cadence = round(context.normal_cadence_minutes)
    return (
        f"** EMERGENCY INVOCATION — trigger: {context.trigger.value} **\n"
        f"Trigger detail: {context.trigger_detail}\n"
        f"Time since last invocation: {minutes}m (normal cadence: ~{cadence}m)"
    )


def prepend_emergency_block(*, header: str, context: EmergencyContext) -> str:
    """Insert the emergency block immediately after the envelope-open line.

    Raises :class:`ValueError` when the input header lacks an envelope-open
    line or carries more than one (a structurally broken header).
    """
    lines = header.split("\n")
    matches = [i for i, line in enumerate(lines) if _ENVELOPE_OPEN_PATTERN.fullmatch(line)]
    if not matches:
        msg = "header is missing the '=== GUARDRAIL STATE ... ===' envelope-open line"
        raise ValueError(msg)
    if len(matches) > 1:
        msg = "header contains multiple envelope-open lines; structurally broken"
        raise ValueError(msg)
    insert_at = matches[0] + 1
    block_lines = render_emergency_block(context).split("\n")
    # Block lines + a single blank-line separator before the next existing line.
    new_lines = lines[:insert_at] + block_lines + [""] + lines[insert_at:]
    return "\n".join(new_lines)
