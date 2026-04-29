"""Minimum position size pre-check (story 01c).

Per-profile economic-viability gate from
``docs/design/06-risk-guardrails/rules-and-limits.md``: positions whose dollar
size falls below the active profile's ``min_position_size_usd`` floor are
rejected before any percentage-based guardrail rule runs. The floor is a
profile-level structural constraint — it doesn't scale with regime, doesn't
have an escalation zone, doesn't have an enforcement-tier hierarchy — so it
ships as a separate pre-check rather than a 20th entry in the rule registry.

Boundary semantics are inclusive: a command at exactly ``min_position_size_usd``
passes. The function is pure (no I/O) and reads only from the passed
``ProfileConfig``.
"""

from dataclasses import dataclass
from enum import StrEnum

from alphamind.config.models.profiles import ProfileConfig


class MinPositionSizeStatus(StrEnum):
    """Pass/fail outcome of the minimum-size pre-check.

    The trailing underscore on ``pass_`` is forced — ``pass`` is a Python
    keyword. Shape mirrors the per-rule ``status`` field in the
    guardrail-evaluation library so consumers can compose results uniformly.
    """

    pass_ = "pass"
    fail = "fail"


@dataclass(frozen=True, slots=True)
class MinPositionSizeResult:
    """Result of a single minimum-size pre-check.

    ``shortfall_usd`` is ``max(min_size_usd - command_size_usd, 0.0)`` — zero
    on pass, positive on fail. Carrying it lets the caller render a useful
    failure message without recomputing.
    """

    status: MinPositionSizeStatus
    command_size_usd: float
    min_size_usd: int
    shortfall_usd: float


def check_min_position_size(
    *, command_size_usd: float, profile: ProfileConfig
) -> MinPositionSizeResult:
    """Reject ``command_size_usd`` when below ``profile.min_position_size_usd``.

    Raises ``ValueError`` if ``command_size_usd <= 0`` — a non-positive command
    is a structural error in the upstream PM/strategist envelope construction;
    surfacing it loudly prevents a silent ``fail`` that masks a corrupt input.
    """
    if command_size_usd <= 0:
        raise ValueError(
            f"command_size_usd must be > 0, got {command_size_usd!r} "
            "(non-positive command indicates upstream envelope-construction error)"
        )
    min_size_usd = profile.min_position_size_usd
    shortfall_usd = max(min_size_usd - command_size_usd, 0.0)
    status = MinPositionSizeStatus.pass_ if shortfall_usd == 0.0 else MinPositionSizeStatus.fail
    return MinPositionSizeResult(
        status=status,
        command_size_usd=command_size_usd,
        min_size_usd=min_size_usd,
        shortfall_usd=shortfall_usd,
    )
