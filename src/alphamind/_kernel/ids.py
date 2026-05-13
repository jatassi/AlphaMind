"""NewType aliases for identifiers across AlphaMind.

Each alias is a distinct type for the type checker but a plain ``str`` at
runtime; passing an :class:`EnvelopeId` where a :class:`PositionId` is expected
fails ``mypy --strict``. Story 04 of the architecture-refactoring work tree
(ALP-460) introduces the types in isolation; story 05a (ALP-461) migrates
consumers to construct/return these typed values at boundaries.

For IDs with a known regex pattern (engine envelope IDs, PM envelope IDs,
their corresponding command IDs), this module exposes constructor functions
that validate the pattern once at the boundary so downstream code can trust
the typed value (python-architecture §D2 — parse-don't-validate). Symbol /
OccSymbol expose only the alias because the validation source (the asset
universe) is defined elsewhere; story 05a will add the constructor when
consumers migrate.
"""

from __future__ import annotations

import re
from typing import NewType

__all__ = [
    "AlpacaOrderId",
    "BracketId",
    "ClientOrderId",
    "CommandId",
    "EnvelopeId",
    "InvocationId",
    "OccSymbol",
    "OrderId",
    "PositionId",
    "Symbol",
    "ThesisId",
    "command_id",
    "envelope_id",
]


OrderId = NewType("OrderId", str)
PositionId = NewType("PositionId", str)
BracketId = NewType("BracketId", str)
CommandId = NewType("CommandId", str)
AlpacaOrderId = NewType("AlpacaOrderId", str)
ClientOrderId = NewType("ClientOrderId", str)
EnvelopeId = NewType("EnvelopeId", str)
InvocationId = NewType("InvocationId", str)
ThesisId = NewType("ThesisId", str)
Symbol = NewType("Symbol", str)
OccSymbol = NewType("OccSymbol", str)


# ---------------------------------------------------------------------------
# Pattern constants — single source of truth for ID shapes already defined
# in scattered downstream modules. Story 05a will switch those modules to
# import these patterns from here.
# ---------------------------------------------------------------------------

_PM_ENVELOPE_ID_PATTERN = re.compile(r"^ENV-(REC|SA|SA-ORD)-[0-9]+$")
"""PM-originated envelope ID: ``ENV-(REC|SA|SA-ORD)-{n}``.

Mirrors ``alphamind.execution.oms.command_ids._ENVELOPE_ID_PATTERN`` —
the JSON Schema pattern in ``docs/design/05-execution-layer/
pm-envelope-schema.md``.
"""

_ENGINE_ENVELOPE_ID_PATTERN = re.compile(r"^MON\.[^.]+\.[0-9]+$")
"""Engine-originated envelope ID: ``MON.{monitor_session_id}.{trigger_id}``.

Mirrors ``alphamind.risk_guardrails.breach_behavior.types._ENVELOPE_ID_PATTERN``
— the JSON Schema pattern in ``docs/design/05-execution-layer/
engine-envelope-schema.md``.
"""

_PM_COMMAND_ID_PATTERN = re.compile(
    r"^inv-(?P<inv>[^.]+)\.(?P<env>ENV-(?:REC|SA|SA-ORD)-[0-9]+)"
    r"\.(?P<ord>[0-9]+)\.(?P<seq>[0-9]+)$"
)
"""PM-originated command ID: ``inv-{invocation}.{envelope}.{ordinal}.{seq}``.

Mirrors ``alphamind.execution.oms.command_ids._PM_COMMAND_ID_PATTERN``.
"""

_ENGINE_COMMAND_ID_PATTERN = re.compile(
    r"^MON\.(?P<session>[^.]+)\.(?P<trigger>[0-9]+)\.(?P<ord>[0-9]+)$"
)
"""Engine-originated command ID: ``MON.{session}.{trigger}.{ordinal}``.

Mirrors ``alphamind.execution.oms.command_ids._ENGINE_COMMAND_ID_PATTERN``.
"""


def envelope_id(value: str) -> EnvelopeId:
    """Construct an :class:`EnvelopeId`, validating the pattern at the boundary.

    Accepts both PM-originated (``ENV-(REC|SA|SA-ORD)-{n}``) and
    engine-originated (``MON.{session}.{trigger}``) envelope IDs — both
    share the :class:`EnvelopeId` type downstream.

    Raises :class:`ValueError` if ``value`` matches neither pattern.
    """
    if not (
        _PM_ENVELOPE_ID_PATTERN.fullmatch(value) or _ENGINE_ENVELOPE_ID_PATTERN.fullmatch(value)
    ):
        msg = (
            f"envelope_id must match {_PM_ENVELOPE_ID_PATTERN.pattern!r} "
            f"or {_ENGINE_ENVELOPE_ID_PATTERN.pattern!r}; got {value!r}"
        )
        raise ValueError(msg)
    return EnvelopeId(value)


def command_id(value: str) -> CommandId:
    """Construct a :class:`CommandId`, validating the pattern at the boundary.

    Accepts both PM-originated (``inv-{inv}.{envelope}.{ord}.{seq}``) and
    engine-originated (``MON.{session}.{trigger}.{ord}``) command IDs — both
    share the :class:`CommandId` type downstream.

    Raises :class:`ValueError` if ``value`` matches neither pattern.
    """
    if not (_PM_COMMAND_ID_PATTERN.fullmatch(value) or _ENGINE_COMMAND_ID_PATTERN.fullmatch(value)):
        msg = (
            f"command_id must match {_PM_COMMAND_ID_PATTERN.pattern!r} "
            f"or {_ENGINE_COMMAND_ID_PATTERN.pattern!r}; got {value!r}"
        )
        raise ValueError(msg)
    return CommandId(value)
