"""Closed-set vocabularies for the counterfactual replay engine (ALP-129).

Each enum mirrors the persisted vocabulary in
``docs/design/05-execution-layer/state-persistence.md`` § Counterfactual
replays. Two members extend the design's base vocabulary deliberately so the
table CHECK constraints (story 02) and the strategist path (story 07) have them
from the start: :attr:`UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED`
and :attr:`ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL`.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Confidence",
    "ExitLeg",
    "ReplayKind",
    "ReplayStatus",
    "UnevaluableReason",
]


class ReplayStatus(StrEnum):
    """Whether the engine produced a counterfactual P/L for the proposal."""

    EVALUATED = "EVALUATED"
    UNEVALUABLE = "UNEVALUABLE"


class ReplayKind(StrEnum):
    """What the replay simulates relative to the PM's decision.

    ``REJECTION`` — PM rejected the proposal; the replay simulates the
    un-rejected form. ``MODIFICATION_ORIGINAL_FORM`` — PM modified the
    proposal; the replay simulates the un-modified original form alongside the
    actual modified-form trade.
    """

    REJECTION = "REJECTION"
    MODIFICATION_ORIGINAL_FORM = "MODIFICATION_ORIGINAL_FORM"


class UnevaluableReason(StrEnum):
    """Why the engine could not produce a counterfactual P/L.

    Set only when :attr:`ReplayStatus.UNEVALUABLE`.

    ``UNSUPPORTED_BRACKET_TYPE`` is part of the design's persisted vocabulary
    and stays defined, but the v2 analyst path does not produce it: analyst
    ``Recommendation`` hard backstops are always underlying-anchored price legs
    or time legs, so the "P/L-on-the-option's-own-price hard backstop" case is
    unreachable from analyst output. The member is reserved; no rule in this
    work tree emits it.

    ``STRATEGIST_POSITION_ACTION_NOT_SUPPORTED`` marks strategist proposals
    whose specific position-action variant the dispatcher cannot replay (e.g.
    ``hold`` / ``maintain`` no-ops, or a ``modify`` pending-order that v2
    declines). Consumed by eligibility (story 04) and the driver (story 08).
    """

    UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
    UNSUPPORTED_BRACKET_TYPE = "UNSUPPORTED_BRACKET_TYPE"
    DATA_MISSING = "DATA_MISSING"
    CORPORATE_ACTION_IN_WINDOW = "CORPORATE_ACTION_IN_WINDOW"
    STRATEGIST_POSITION_ACTION_NOT_SUPPORTED = "STRATEGIST_POSITION_ACTION_NOT_SUPPORTED"


class ExitLeg(StrEnum):
    """Which exit condition fired in the simulated trade.

    ``STRATEGIST_CLOSE_AT_PROPOSAL`` is the one-shot exit leg for strategist
    CLOSE and REDUCE replays, which exit at the proposal-following bar rather
    than via a bracket walk (story 07). ADJUST-BRACKET and ADD strategist
    replays use the ordinary ``TARGET_HIT`` / ``STOP_HIT`` / ``TIME_STOP_FIRED``
    / ``ENTRY_WINDOW_EXPIRED_UNFILLED`` legs.
    """

    TARGET_HIT = "TARGET_HIT"
    STOP_HIT = "STOP_HIT"
    TIME_STOP_FIRED = "TIME_STOP_FIRED"
    ENTRY_WINDOW_EXPIRED_UNFILLED = "ENTRY_WINDOW_EXPIRED_UNFILLED"
    STRATEGIST_CLOSE_AT_PROPOSAL = "STRATEGIST_CLOSE_AT_PROPOSAL"


class Confidence(StrEnum):
    """Replay confidence; low-confidence replays are excluded from aggregated
    PM accuracy metrics at query time per the engine's confidence rule.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
