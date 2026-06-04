"""Canonical OMS command-ID derivation utility — story 01b (ALP-371).

Pure, stateless primitives for deriving and parsing OMS command IDs as
specified in :doc:`docs/design/oms-command-ids.md`. Two derivation functions
(PM-originated / engine-originated), a helper that counts post-rejection
modifications on a :class:`PMEnvelope`, and inverse parsers.

The module is the canonical home for this logic. The submit_envelope wrapper
(:mod:`alphamind.decision.portfolio_manager.submit_envelope`) calls
:func:`derive_pm_command_id`; the continuous-monitor work tree consumes
:func:`derive_engine_command_id` when emitting envelopes.

After ALP-458 the :class:`PMEnvelope` type lives in
:mod:`alphamind.commands.pm_envelope` — execution imports the wire-format
kernel downward to read the modification list for ``attempt_seq``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from alphamind._kernel.ids import EnvelopeId, InvocationId, ThesisId
from alphamind.commands.pm_envelope import PMEnvelope

__all__ = [
    "EngineCommandIdComponents",
    "PMCommandIdComponents",
    "base_command_id",
    "compute_attempt_seq",
    "derive_engine_command_id",
    "derive_pm_command_id",
    "is_engine_originated",
    "is_pm_originated",
    "parse_engine_command_id",
    "parse_pm_command_id",
    "synthesize_id_suffix",
]


# ---------------------------------------------------------------------------
# Broker-carried link (ALP-844 / ADR 0002)
# ---------------------------------------------------------------------------
#
# Every AlphaMind-originated command id carries the durable Intent foreign key
# — the originating thesis (*why*) and invocation (*when*) — so the fill the
# broker echoes it on is self-attributing with no local order row. The link
# segments are appended after the legacy base id with a ``~`` delimiter that
# never appears inside a thesis id (``THE-<ticker>-<32hex>``, characters
# ``[A-Z0-9.-]``) or an invocation id (``inv-…Z-<hex>``), so a dot-bearing
# multi-share-class ticker (``THE-BRK.B-…``) round-trips unambiguously.
#
# The ``~`` segments live *after* the base id, and :func:`synthesize_id_suffix`
# / :func:`base_command_id` strip them before any id-suffix derivation. That
# keeps the Phase-2 minted position / order / thesis ids — all of which hash
# the command id via :func:`synthesize_id_suffix` — byte-for-byte identical to
# the pre-ALP-844 base-id derivation, so embedding the link does not perturb
# downstream id minting.

# A thesis id reaches the persisted thesis row: ``THE-<ticker>-<32hex>``.
# ``<ticker>`` may carry a single dot (``BRK.B``); the segment is anchored by
# its literal ``THE-`` head and the ``~`` neighbours so the dot never makes the
# grammar ambiguous.
_THESIS_SEGMENT = r"THE-[A-Za-z0-9.]+-[0-9a-f]{32}"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_ENVELOPE_ID_PATTERN = re.compile(r"^ENV-(REC|SA|SA-ORD)-[0-9]+$")
_PM_COMMAND_ID_PATTERN = re.compile(
    r"^inv-(?P<inv>[^.~]+)\.(?P<env>ENV-(?:REC|SA|SA-ORD)-[0-9]+)"
    r"\.(?P<ord>[0-9]+)\.(?P<seq>[0-9]+)"
    r"~the-(?P<thesis>" + _THESIS_SEGMENT + r")$"
)
_ENGINE_COMMAND_ID_PATTERN = re.compile(
    r"^MON\.(?P<session>[^.~]+)\.(?P<trigger>[0-9]+)\.(?P<ord>[0-9]+)"
    r"~the-(?P<thesis>" + _THESIS_SEGMENT + r")"
    r"~inv-(?P<inv>[^.~]+)$"
)

# The base id (no broker-carried link) is the substring up to the first ``~``.
# Stripping it keeps id-suffix derivation stable across the ALP-844 change.
_LINK_DELIMITER = "~"


# ---------------------------------------------------------------------------
# Component models (frozen dataclass — internal carriers per ALP-476 / 10c)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PMCommandIdComponents:
    """Decomposed PM-originated command ID — output of :func:`parse_pm_command_id`.

    ``invocation_id`` is returned **without** the ``inv-`` prefix to match the
    parameter shape that :func:`derive_pm_command_id` accepts when the prefix
    is omitted. ``thesis_id`` is the broker-carried Intent FK (ALP-844) that
    makes a fill self-attributing.
    """

    invocation_id: InvocationId
    envelope_id: EnvelopeId
    command_ordinal: int
    attempt_seq: int
    thesis_id: ThesisId


@dataclass(frozen=True, slots=True)
class EngineCommandIdComponents:
    """Decomposed engine-originated command ID — output of
    :func:`parse_engine_command_id`.

    Carries the broker-carried Intent FK (ALP-844) so an engine-originated
    close self-attributes by thesis (*why*) + invocation (*when*) too.
    """

    monitor_session_id: str
    trigger_id: int
    command_ordinal: int
    thesis_id: ThesisId
    invocation_id: InvocationId


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------


def _require_thesis(thesis_id: ThesisId) -> str:
    """Validate the broker-carried thesis FK and return it as a plain ``str``.

    A missing / empty thesis is structurally impossible for an AlphaMind order
    — every such order originates from a thesis (ADR 0002). An absent (``None``)
    or empty value raises :class:`ValueError`; a malformed one is rejected by
    the ``THE-<ticker>-<32hex>`` grammar so it never round-trips a bad FK.
    """
    if not thesis_id:
        raise ValueError(
            "thesis_id is required — an AlphaMind order has no thesis-less path "
            "(ADR 0002: attribution rides a broker-carried link)"
        )
    text = str(thesis_id)
    if not re.fullmatch(_THESIS_SEGMENT, text):
        raise ValueError(
            f"thesis_id must match {_THESIS_SEGMENT!r} (THE-<ticker>-<32hex>), got {thesis_id!r}"
        )
    return text


def _require_invocation(invocation_id: str) -> str:
    """Validate a non-empty invocation id and return it without the ``inv-`` prefix.

    The broker-carried link's invocation segment never carries the ``inv-``
    prefix (the grammar re-adds it on parse), so a dotted / tilde-bearing /
    empty value raises before it can corrupt the id.
    """
    if not invocation_id:
        raise ValueError("invocation_id is required and must be non-empty")
    bare = invocation_id.removeprefix("inv-")
    if not bare or "." in bare or _LINK_DELIMITER in bare:
        raise ValueError(
            f"invocation_id must be non-empty and free of '.'/'{_LINK_DELIMITER}', "
            f"got {invocation_id!r}"
        )
    return bare


def derive_pm_command_id(
    *,
    invocation_id: str,
    envelope_id: str,
    command_ordinal: int,
    attempt_seq: int,
    thesis_id: ThesisId,
) -> str:
    """Format a PM-originated command ID carrying the broker-carried Intent FK.

    Pattern: ``inv-{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}``
    ``~the-{thesis_id}``. Prefixes ``inv-`` only if ``invocation_id`` does not
    already start with it (mirrors the gate in the submit_envelope helper). The
    ``~the-`` segment carries the durable thesis FK (*why*); the ``invocation``
    segment is the *when*, so the derived id round-trips both (ALP-844).

    Raises :class:`ValueError` for negative ``command_ordinal`` / ``attempt_seq``,
    an ``envelope_id`` that does not match ``^ENV-(REC|SA|SA-ORD)-[0-9]+$``, an
    empty / dotted ``invocation_id``, or a missing / malformed ``thesis_id``. A
    thesis-less order is unrepresentable — there is no sentinel fallback.
    """
    if command_ordinal < 0:
        raise ValueError(f"command_ordinal must be non-negative, got {command_ordinal}")
    if attempt_seq < 0:
        raise ValueError(f"attempt_seq must be non-negative, got {attempt_seq}")
    if not _ENVELOPE_ID_PATTERN.match(envelope_id):
        raise ValueError(
            f"envelope_id must match ^ENV-(REC|SA|SA-ORD)-[0-9]+$, got {envelope_id!r}"
        )
    bare_invocation = _require_invocation(invocation_id)
    thesis = _require_thesis(thesis_id)
    return f"inv-{bare_invocation}.{envelope_id}.{command_ordinal}.{attempt_seq}~the-{thesis}"


def derive_engine_command_id(
    *,
    monitor_session_id: str,
    trigger_id: int,
    thesis_id: ThesisId,
    invocation_id: str,
    command_ordinal: int = 0,
) -> str:
    """Format an engine-originated command ID carrying the broker-carried Intent FK.

    Pattern: ``MON.{monitor_session_id}.{trigger_id}.{command_ordinal}``
    ``~the-{thesis_id}~inv-{invocation_id}``. Default ``command_ordinal=0`` per
    design (a trigger produces a single CLOSE; cascades produce multiple
    envelopes, not multiple commands). The ``~the-`` / ``~inv-`` segments carry
    the durable thesis (*why*) + invocation (*when*) FK so an engine-fired
    close self-attributes end-to-end (ALP-844).

    Raises :class:`ValueError` for an empty or dotted ``monitor_session_id``, a
    negative ``trigger_id`` / ``command_ordinal``, a missing / malformed
    ``thesis_id``, or an empty / dotted ``invocation_id``. A thesis-less
    engine close is unrepresentable — there is no sentinel fallback.
    """
    if not monitor_session_id or "." in monitor_session_id or _LINK_DELIMITER in monitor_session_id:
        raise ValueError(
            f"monitor_session_id must be non-empty and free of '.'/'{_LINK_DELIMITER}', "
            f"got {monitor_session_id!r}"
        )
    if trigger_id < 0:
        raise ValueError(f"trigger_id must be non-negative, got {trigger_id}")
    if command_ordinal < 0:
        raise ValueError(f"command_ordinal must be non-negative, got {command_ordinal}")
    thesis = _require_thesis(thesis_id)
    bare_invocation = _require_invocation(invocation_id)
    return (
        f"MON.{monitor_session_id}.{trigger_id}.{command_ordinal}"
        f"~the-{thesis}~inv-{bare_invocation}"
    )


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
    ``invocation_id`` and the broker-carried ``thesis_id`` resolved. Raises
    :class:`ValueError` for any malformed input.
    """
    match = _PM_COMMAND_ID_PATTERN.match(command_id)
    if match is None:
        raise ValueError(f"command_id does not match PM-originated pattern, got {command_id!r}")
    return PMCommandIdComponents(
        invocation_id=InvocationId(match["inv"]),
        envelope_id=EnvelopeId(match["env"]),
        command_ordinal=int(match["ord"]),
        attempt_seq=int(match["seq"]),
        thesis_id=ThesisId(match["thesis"]),
    )


def parse_engine_command_id(command_id: str) -> EngineCommandIdComponents:
    """Inverse of :func:`derive_engine_command_id`.

    Resolves the broker-carried ``thesis_id`` + ``invocation_id`` (the latter
    without the ``inv-`` prefix). Raises :class:`ValueError` for any malformed
    input.
    """
    match = _ENGINE_COMMAND_ID_PATTERN.match(command_id)
    if match is None:
        raise ValueError(f"command_id does not match engine-originated pattern, got {command_id!r}")
    return EngineCommandIdComponents(
        monitor_session_id=match["session"],
        trigger_id=int(match["trigger"]),
        command_ordinal=int(match["ord"]),
        thesis_id=ThesisId(match["thesis"]),
        invocation_id=InvocationId(match["inv"]),
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


# ---------------------------------------------------------------------------
# Position / order id suffix synthesis
# ---------------------------------------------------------------------------


def base_command_id(command_id: str) -> str:
    """Return *command_id* with any broker-carried link segments removed.

    The base id is the legacy ``inv-…`` / ``MON.…`` form up to the first ``~``
    link delimiter (ALP-844). For an id that never carried a link the input is
    returned unchanged. Id-suffix derivation operates on the base so that the
    Phase-2 minted thesis / position / order ids — which hash the command id —
    stay identical to the pre-link derivation regardless of which thesis FK the
    id carries.
    """
    return command_id.split(_LINK_DELIMITER, 1)[0]


def synthesize_id_suffix(command_id: str) -> str:
    """Return the stable 32-hex suffix the OMS appends to position / order ids
    minted from *command_id*.

    Both the submit_envelope wrapper's acknowledgment and the Phase 2 writeback
    layer derive their position / order identifiers from the same suffix so the
    LLM sees identifiers that match what landed on the persisted rows. The
    broker-carried link (ALP-844) is stripped via :func:`base_command_id`
    first, so the suffix — and thus every minted id — is a function of the base
    command id alone and is unchanged by embedding the thesis / invocation FK.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, base_command_id(command_id)).hex
