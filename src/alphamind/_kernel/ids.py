"""NewType aliases for identifiers across AlphaMind.

Each alias is a distinct type for the type checker but a plain ``str`` at
runtime; passing an :class:`EnvelopeId` where a :class:`PositionId` is
expected fails ``mypy --strict``. Consumers construct typed values at
boundaries via the ``envelope_id`` / ``command_id`` / ``recommendation_id``
/ ``make_symbol`` / ``make_occ_symbol`` constructors, which validate the
pattern once so downstream code can trust the typed value
(python-architecture §D2 — parse-don't-validate). Bare ``Symbol`` /
``OccSymbol`` aliases remain public so test fixtures can construct without
re-validating known-good inputs.
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
    "RecommendationId",
    "Symbol",
    "ThesisId",
    "command_id",
    "envelope_id",
    "make_occ_symbol",
    "make_symbol",
    "recommendation_id",
]


OrderId = NewType("OrderId", str)
PositionId = NewType("PositionId", str)
BracketId = NewType("BracketId", str)
CommandId = NewType("CommandId", str)
AlpacaOrderId = NewType("AlpacaOrderId", str)
ClientOrderId = NewType("ClientOrderId", str)
EnvelopeId = NewType("EnvelopeId", str)
InvocationId = NewType("InvocationId", str)
RecommendationId = NewType("RecommendationId", str)
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


_ANALYST_RECOMMENDATION_ID_PATTERN = re.compile(r"^REC-[0-9]+$")
"""Analyst-originated recommendation ID: ``REC-{n}``.

Mirrors the pattern in
``docs/design/04-decision-layer/analyst-output-schema.md``.
"""

_STRATEGIST_RECOMMENDATION_ID_PATTERN = re.compile(r"^SA(-ORD)?-[0-9]+$")
"""Strategist-originated recommendation ID: ``SA-{n}`` or ``SA-ORD-{n}``.

Mirrors the pattern in
``docs/design/04-decision-layer/strategist-output-schema.md``.
"""

_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)?$")
"""US-equity ticker: 1-10 chars, leading uppercase letter, optional single dot.

Tighter than the canonical asset-universe validator
``alphamind.config.models.assets._TICKER_RE`` — disallows consecutive dots,
leading/trailing dots, and lowercase. Caps total length at 10 chars so
"garbage with spaces"-length strings fail at the boundary. Accepts plain
tickers (``AAPL``, ``NVDA``), multi-share-class tickers with one dot
(``BRK.B``, ``BF.B``), and the rare single-letter ticker (``A``, ``F``).
Rejects empty string, whitespace, lowercase, leading/trailing dot,
consecutive dots.

The length cap (10) is enforced by :func:`make_symbol` rather than the
regex — embedding ``{,10}`` inside the alternation made the pattern much
harder to read for a marginal benefit.
"""

_OCC_SYMBOL_PATTERN = re.compile(r"^(?=.{16,21}$)[A-Z]{1,6} *\d{6}[CP]\d{8}$")
"""OCC option symbol: ``{root:1-6}[ *]{YYMMDD:6}{CP:1}{strike:8}``.

16-21 chars total. Root is uppercase alphabetic (no digits, no dot — OCC
encodes share-class tickers without the dot), optionally right-padded with
trailing spaces inside a fixed 21-char root field; the production builder
``alphamind.execution.broker_adapter.order_options.build_occ_symbol`` emits
the canonical space-padded 21-char form (``"NVDA  260619C00800000"``),
while the compressed form (``"AAPL250620C00200000"``) is also accepted.
Expiry is six digits. ``C`` or ``P`` denotes call/put. Strike is the
8-digit zero-padded integer ``price * 1000`` (e.g., ``$200.00`` strike =>
``00200000``).
"""


_SYMBOL_MAX_LEN = 10
"""Length cap enforced by :func:`make_symbol` alongside :data:`_SYMBOL_PATTERN`."""


def make_symbol(value: str) -> Symbol:
    """Construct a :class:`Symbol`, validating the US-equity ticker pattern.

    Accepts uppercase tickers up to 10 chars, optionally containing a single
    dot for multi-share-class tickers (``BRK.B``). Raises :class:`ValueError`
    with the offending input on mismatch.
    """
    if len(value) > _SYMBOL_MAX_LEN or not _SYMBOL_PATTERN.fullmatch(value):
        msg = (
            f"make_symbol must match {_SYMBOL_PATTERN.pattern!r} and be "
            f"<= {_SYMBOL_MAX_LEN} chars; got {value!r}"
        )
        raise ValueError(msg)
    return Symbol(value)


def make_occ_symbol(value: str) -> OccSymbol:
    """Construct an :class:`OccSymbol`, validating the OCC option-symbol pattern.

    Pattern: ``^(?=.{16,21}$)[A-Z]{1,6} *\\d{6}[CP]\\d{8}$`` — root +
    optional trailing space padding + YYMMDD expiry + ``C``/``P`` + 8-digit
    zero-padded strike (price * 1000). Accepts both the canonical
    space-padded 21-char form emitted by
    :func:`alphamind.execution.broker_adapter.order_options.build_occ_symbol`
    (``"NVDA  260619C00800000"``) and the compressed form without padding
    (``"AAPL250620C00200000"``). Raises :class:`ValueError` with the
    offending input on mismatch.
    """
    if not _OCC_SYMBOL_PATTERN.fullmatch(value):
        msg = f"make_occ_symbol must match {_OCC_SYMBOL_PATTERN.pattern!r}; got {value!r}"
        raise ValueError(msg)
    return OccSymbol(value)


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


def recommendation_id(value: str) -> RecommendationId:
    """Construct a :class:`RecommendationId`, validating the pattern at the boundary.

    Accepts both analyst-originated (``REC-{n}``) and strategist-originated
    (``SA-{n}`` or ``SA-ORD-{n}``) recommendation IDs — both share the
    :class:`RecommendationId` type downstream.

    Raises :class:`ValueError` if ``value`` matches neither pattern.
    """
    if not (
        _ANALYST_RECOMMENDATION_ID_PATTERN.fullmatch(value)
        or _STRATEGIST_RECOMMENDATION_ID_PATTERN.fullmatch(value)
    ):
        msg = (
            f"recommendation_id must match {_ANALYST_RECOMMENDATION_ID_PATTERN.pattern!r} "
            f"or {_STRATEGIST_RECOMMENDATION_ID_PATTERN.pattern!r}; got {value!r}"
        )
        raise ValueError(msg)
    return RecommendationId(value)
