"""Hard rejection payload composition (story 04c).

Pure function that assembles the synchronous T3 ``HardRejectionPayload`` the
engine returns to the PM when one or more guardrail rules fail under a
proposed command. Consumes the guardrail-evaluation library's per-rule
projection result via the structural Protocols ``RuleProjectionProtocol`` /
``LibraryOutputProtocol`` so this story stays decoupled from the upstream
package's exact module path; the Protocols match the shipped
``RuleProjection`` and ``LibraryOutput`` shapes verbatim.

See ``docs/implementation/06-risk-guardrails/breach-behavior/04c-hard-rejection-payload.md``
for the authoritative contract and worked example.
"""

from __future__ import annotations

from typing import Protocol

from alphamind.risk_guardrails.breach_behavior.types import (
    HardRejectionPayload,
    RejectionRuleEntry,
)

_FAIL_STATUS = "FAIL"


class RuleProjectionProtocol(Protocol):
    """Structural shape of one per-rule projection consumed by this primitive.

    Matches the guardrail-evaluation ``RuleProjection`` dataclass field-for-field.
    ``status`` is documented as the string label (``"PASS"`` / ``"WARNING"`` /
    ``"FAIL"``); production callers whose projection carries a ``Status`` enum
    pass ``rule_projection.status.value`` (or an equivalent adapter) at the
    call site. Attributes are declared as read-only ``@property`` so frozen
    dataclasses (the production ``RuleProjection`` and any test stubs) satisfy
    the structural contract.
    """

    @property
    def rule(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def current(self) -> float: ...
    @property
    def limit(self) -> float: ...
    @property
    def projected_after(self) -> float: ...
    @property
    def headroom_remaining(self) -> float: ...
    @property
    def unit(self) -> str: ...


class LibraryOutputProtocol(Protocol):
    """Structural shape of the guardrail-evaluation library's output."""

    @property
    def per_rule(self) -> tuple[RuleProjectionProtocol, ...]: ...


def compose_hard_rejection_payload(
    *,
    rejected_command_id: str,
    library_output: LibraryOutputProtocol,
    library_output_after_hypothetical_compliance: LibraryOutputProtocol,
    suggested_modification: str,
) -> HardRejectionPayload:
    """Assemble the synchronous T3 rejection payload returned to the PM.

    Args:
        rejected_command_id: The OMS command ID of the command being rejected
            (e.g., ``"PM.{invocation}.{ordinal}.1"``).
        library_output: The guardrail-evaluation library's output for the
            *as-submitted* command. Must carry at least one ``per_rule`` entry
            with ``status == "FAIL"`` — otherwise the command should not have
            been rejected.
        library_output_after_hypothetical_compliance: The library's output for
            the *post-modification* command (i.e., the suggested-modification
            result). Carries the projected per-rule headroom the PM sees if it
            adopts the suggestion.
        suggested_modification: PM-actionable text. Caller-constructed; this
            primitive consumes it verbatim.

    Returns:
        A frozen ``HardRejectionPayload`` carrying the breaching-rule entries,
        the suggested modification, and the post-compliance headroom for every
        rule.

    Raises:
        ValueError: when ``library_output.per_rule`` has zero entries with
            ``status == "FAIL"`` (no rejection warranted), when either output's
            ``per_rule`` is empty, or when ``suggested_modification`` is empty
            or whitespace-only.
    """
    if not suggested_modification.strip():
        msg = "suggested_modification must not be empty or whitespace-only"
        raise ValueError(msg)
    if len(library_output.per_rule) == 0:
        msg = "library_output.per_rule is empty; nothing to reject"
        raise ValueError(msg)
    if len(library_output_after_hypothetical_compliance.per_rule) == 0:
        msg = (
            "library_output_after_hypothetical_compliance.per_rule is empty; "
            "post-compliance picture required"
        )
        raise ValueError(msg)

    breaching_rules = tuple(
        _rejection_entry(rule, clamp_overage_at_zero=False)
        for rule in library_output.per_rule
        if rule.status == _FAIL_STATUS
    )
    if len(breaching_rules) == 0:
        msg = "no rules failed; rejection payload requires at least one FAIL"
        raise ValueError(msg)

    headroom_after = tuple(
        _rejection_entry(rule, clamp_overage_at_zero=True)
        for rule in library_output_after_hypothetical_compliance.per_rule
    )

    return HardRejectionPayload(
        rejected_command_id=rejected_command_id,
        breaching_rules=breaching_rules,
        suggested_modification=suggested_modification,
        headroom_after_hypothetical_compliance=headroom_after,
    )


def _rejection_entry(
    rule: RuleProjectionProtocol,
    *,
    clamp_overage_at_zero: bool,
) -> RejectionRuleEntry:
    """Project one rule into a ``RejectionRuleEntry``.

    The breaching set carries every FAIL'd rule's signed overage (always
    positive by construction). The post-compliance set clamps overage at zero
    so cured rules show zero overage; only any rule still over-limit after the
    suggested modification surfaces a positive overage.
    """
    raw_overage = rule.projected_after - rule.limit
    overage = max(0.0, raw_overage) if clamp_overage_at_zero else raw_overage
    return RejectionRuleEntry(
        rule_id=rule.rule,
        current_value=rule.current,
        limit_value=rule.limit,
        projected_after=rule.projected_after,
        overage=overage,
        headroom_remaining=rule.headroom_remaining,
        unit=rule.unit,
    )


__all__ = [
    "LibraryOutputProtocol",
    "RuleProjectionProtocol",
    "compose_hard_rejection_payload",
]
