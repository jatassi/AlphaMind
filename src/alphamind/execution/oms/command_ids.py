"""Canonical OMS command-ID derivation utility — story 01b (ALP-371).

Pure, stateless primitives for deriving and parsing OMS command IDs as
specified in :doc:`docs/design/oms-command-ids.md`. Two derivation functions
(PM-originated / engine-originated), a helper that counts post-rejection
modifications on a :class:`PMEnvelope`, and inverse parsers.

The module is the canonical home for this logic. The engine-stub
(:mod:`alphamind.decision.portfolio_manager.submit_envelope`) calls
:func:`derive_pm_command_id`; the continuous-monitor work tree consumes
:func:`derive_engine_command_id` when emitting envelopes.

After ALP-458 the :class:`PMEnvelope` type lives in
:mod:`alphamind.commands.pm_envelope` — execution imports the wire-format
kernel downward to read the modification list for ``attempt_seq``.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from alphamind._kernel.ids import EnvelopeId, InvocationId
from alphamind.commands.pm_envelope import PMEnvelope

__all__ = [
    "EngineCommandIdComponents",
    "PMCommandIdComponents",
    "compute_attempt_seq",
    "derive_engine_command_id",
    "derive_pm_command_id",
    "is_engine_originated",
    "is_pm_originated",
    "parse_engine_command_id",
    "parse_pm_command_id",
]


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_ENVELOPE_ID_PATTERN = re.compile(r"^ENV-(REC|SA|SA-ORD)-[0-9]+$")
_PM_COMMAND_ID_PATTERN = re.compile(
    r"^inv-(?P<inv>[^.]+)\.(?P<env>ENV-(?:REC|SA|SA-ORD)-[0-9]+)"
    r"\.(?P<ord>[0-9]+)\.(?P<seq>[0-9]+)$"
)
_ENGINE_COMMAND_ID_PATTERN = re.compile(
    r"^MON\.(?P<session>[^.]+)\.(?P<trigger>[0-9]+)\.(?P<ord>[0-9]+)$"
)


# ---------------------------------------------------------------------------
# Component models (frozen, Pydantic)
# ---------------------------------------------------------------------------


class PMCommandIdComponents(BaseModel):
    """Decomposed PM-originated command ID — output of :func:`parse_pm_command_id`.

    ``invocation_id`` is returned **without** the ``inv-`` prefix to match the
    parameter shape that :func:`derive_pm_command_id` accepts when the prefix
    is omitted.
    """

    model_config = ConfigDict(frozen=True)

    invocation_id: InvocationId
    envelope_id: EnvelopeId
    command_ordinal: int
    attempt_seq: int


class EngineCommandIdComponents(BaseModel):
    """Decomposed engine-originated command ID — output of
    :func:`parse_engine_command_id`."""

    model_config = ConfigDict(frozen=True)

    monitor_session_id: str
    trigger_id: int
    command_ordinal: int


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------


def derive_pm_command_id(
    *,
    invocation_id: str,
    envelope_id: str,
    command_ordinal: int,
    attempt_seq: int,
) -> str:
    """Format a PM-originated command ID per ``oms-command-ids.md``.

    Pattern: ``inv-{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}``.
    Prefixes ``inv-`` only if ``invocation_id`` does not already start with it
    (mirrors the gate in the existing engine-stub helper).

    Raises :class:`ValueError` for negative ``command_ordinal`` / ``attempt_seq``
    or an ``envelope_id`` that does not match
    ``^ENV-(REC|SA|SA-ORD)-[0-9]+$``.
    """
    if command_ordinal < 0:
        raise ValueError(f"command_ordinal must be non-negative, got {command_ordinal}")
    if attempt_seq < 0:
        raise ValueError(f"attempt_seq must be non-negative, got {attempt_seq}")
    if not _ENVELOPE_ID_PATTERN.match(envelope_id):
        raise ValueError(
            f"envelope_id must match ^ENV-(REC|SA|SA-ORD)-[0-9]+$, got {envelope_id!r}"
        )
    prefix = invocation_id if invocation_id.startswith("inv-") else f"inv-{invocation_id}"
    return f"{prefix}.{envelope_id}.{command_ordinal}.{attempt_seq}"


def derive_engine_command_id(
    *,
    monitor_session_id: str,
    trigger_id: int,
    command_ordinal: int = 0,
) -> str:
    """Format an engine-originated command ID per ``oms-command-ids.md``.

    Pattern: ``MON.{monitor_session_id}.{trigger_id}.{command_ordinal}``.
    Default ``command_ordinal=0`` per design (a trigger produces a single
    CLOSE; cascades produce multiple envelopes, not multiple commands).

    Raises :class:`ValueError` for an empty or dotted ``monitor_session_id``,
    a negative ``trigger_id``, or a negative ``command_ordinal``.
    """
    if not monitor_session_id or "." in monitor_session_id:
        raise ValueError(
            f"monitor_session_id must be non-empty and not contain '.', got {monitor_session_id!r}"
        )
    if trigger_id < 0:
        raise ValueError(f"trigger_id must be non-negative, got {trigger_id}")
    if command_ordinal < 0:
        raise ValueError(f"command_ordinal must be non-negative, got {command_ordinal}")
    return f"MON.{monitor_session_id}.{trigger_id}.{command_ordinal}"


# ---------------------------------------------------------------------------
# attempt_seq computation
# ---------------------------------------------------------------------------


def compute_attempt_seq(envelope: PMEnvelope) -> int:
    """Count post-rejection modifications on an envelope.

    Per ``oms-command-ids.md`` § ``attempt_seq`` computation: the first
    submission has ``attempt_seq=0``; each guardrail-rejection-driven
    modification increments. Pre-submission modifications (e.g.,
    ``conviction_disagreement``) do not affect ``attempt_seq``.
    """
    return sum(1 for m in envelope.modifications if m.phase == "post_rejection")


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_pm_command_id(command_id: str) -> PMCommandIdComponents:
    """Inverse of :func:`derive_pm_command_id`.

    Returns the decomposed components with the ``inv-`` prefix stripped from
    ``invocation_id``. Raises :class:`ValueError` for any malformed input.
    """
    match = _PM_COMMAND_ID_PATTERN.match(command_id)
    if match is None:
        raise ValueError(f"command_id does not match PM-originated pattern, got {command_id!r}")
    return PMCommandIdComponents(
        invocation_id=InvocationId(match["inv"]),
        envelope_id=EnvelopeId(match["env"]),
        command_ordinal=int(match["ord"]),
        attempt_seq=int(match["seq"]),
    )


def parse_engine_command_id(command_id: str) -> EngineCommandIdComponents:
    """Inverse of :func:`derive_engine_command_id`.

    Raises :class:`ValueError` for any malformed input.
    """
    match = _ENGINE_COMMAND_ID_PATTERN.match(command_id)
    if match is None:
        raise ValueError(f"command_id does not match engine-originated pattern, got {command_id!r}")
    return EngineCommandIdComponents(
        monitor_session_id=match["session"],
        trigger_id=int(match["trigger"]),
        command_ordinal=int(match["ord"]),
    )


# ---------------------------------------------------------------------------
# Discriminators
# ---------------------------------------------------------------------------


def is_pm_originated(command_id: str) -> bool:
    """``True`` iff ``command_id`` is a structurally-valid PM-originated ID.

    Does not raise — returns ``False`` for any malformed input.
    """
    return _PM_COMMAND_ID_PATTERN.match(command_id) is not None


def is_engine_originated(command_id: str) -> bool:
    """``True`` iff ``command_id`` is a structurally-valid engine-originated ID.

    Does not raise — returns ``False`` for any malformed input.
    """
    return _ENGINE_COMMAND_ID_PATTERN.match(command_id) is not None
