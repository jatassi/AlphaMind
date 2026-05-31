"""Per-block output envelope and structured-text formatter.

Every distillation output is an :class:`OutputBlock` carrying a freshness
timestamp, a calibration tag (story 04), zero or more anomaly flags, an
optional regime annotation, and a free-shape payload. The envelope is
partitioned by consumer (sector analyst agents, the synthesizer's
correlation/regime brief, all agents) per
``docs/design/02-distillation-layer/external.md`` § Output format.

The :func:`format_block` renderer produces structured text deterministic to
the byte: dictionaries iterate sorted, floats use a fixed precision, and the
current wall clock is never embedded. The "byte-identical on repeated calls"
property is what makes the invocation archive diffable per
``docs/architecture/infrastructure.md`` § Invocation archive — operators
reading historical archives expect the format to be stable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from alphamind.distillation._calibration_core import CalibrationState


class OutputAudience(StrEnum):
    """Partition targets for distillation output blocks.

    The five members map 1:1 to the partition targets named in
    ``docs/design/02-distillation-layer/external.md`` § Output format:
    three sector analyst agent audiences, the synthesizer's correlation /
    regime brief audience, and the universal broadcast that every analysis
    agent receives.
    """

    SECTOR_TECH_SEMIS = "sector_tech_semis"
    SECTOR_FINANCIALS = "sector_financials"
    SECTOR_ENERGY = "sector_energy"
    CORRELATION_REGIME_BRIEF = "correlation_regime_brief"
    UNIVERSAL_BROADCAST = "universal_broadcast"


# ---------------------------------------------------------------------------
# Anomaly flag carried in the envelope
# ---------------------------------------------------------------------------


AnomalySeverity = Literal[
    "investigate_now",
    "investigate_if_persists",
    "note_for_context",
]
"""Three-level severity taxonomy.

Matches the analyst-side severity strings in
``docs/design/03-analysis-layer/domain-researchers/tech-semis.md``
§ Domain researcher output contract so domain researchers can pass the level
through unchanged.
"""


SEVERITY_ORDER: tuple[AnomalySeverity, ...] = (
    "investigate_now",
    "investigate_if_persists",
    "note_for_context",
)
"""Total order over :data:`AnomalySeverity` — earlier = stronger.

The single source of truth for severity ranking, consumed by both the
aggregation layer (sort order for the per-audience anomaly summary) and
the publishing-layer cap (comparing producer severity to the
calibration-state ceiling).
"""


def severity_rank(severity: AnomalySeverity) -> int:
    """Return the position of ``severity`` in :data:`SEVERITY_ORDER`.

    Smaller index = stronger severity. The absolute integer is irrelevant
    — the rank only ever participates in comparisons.
    """
    return SEVERITY_ORDER.index(severity)


@dataclass(frozen=True)
class AnomalyFlag:
    """A single anomaly attached to an :class:`OutputBlock`.

    Anomaly flags travel inside the envelope rather than as a separate
    output channel because
    ``docs/design/02-distillation-layer/external.md`` § 3 explicitly groups
    them inline with the indicator block they accompany — anomalies belong
    with the data that triggered them.
    """

    name: str
    magnitude: float
    severity: AnomalySeverity


# ---------------------------------------------------------------------------
# Per-block envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputBlock:
    """One distillation output with its envelope.

    Field summary (see ``docs/implementation/02-distillation-layer/05-output-envelope.md``
    § Scope for the full contract):

    - ``block_id`` — ``<category>.<short_name>`` per the convention pinned in
      this story; ``<category>`` mirrors the
      ``docs/design/02-distillation-layer/external.md`` § 2 heading shorthand
      (``q1``, ``q3``, ``q6``, ``q7``, ``q12``, ``qual``).
    - ``audience`` — one or more partition targets; an indicator may broadcast
      to multiple consumers (e.g. a regime-relevant flag goes to a sector
      audience and to :attr:`OutputAudience.UNIVERSAL_BROADCAST`). Empty sets
      are rejected in :meth:`__post_init__` because every block must declare
      at least one consumer.
    - ``freshness_ts`` — UTC timestamp of the most recent data point that fed
      the block, **not** the invocation start. The renderer never embeds the
      current wall clock so the invocation archive diffs cleanly across runs.
    - ``calibration_state`` / ``bootstrap_reason`` — the story-04 tag pair.
      ``bootstrap_reason`` is ``None`` when the state is
      :attr:`CalibrationState.CALIBRATED` and an operator-readable
      string otherwise. The Python field keeps the legacy
      ``bootstrap_reason`` name (carried through ~30 dataclasses); the
      operator-facing renderer (``format_block``) labels it ``reason``
      per ALP-540 since the state now distinguishes ``accumulating``
      from ``unavailable``.
    - ``payload`` — free-shape per-category content. The formatter renders it
      deterministically (sorted keys, fixed float precision) so the envelope
      contract holds regardless of payload shape.
    - ``anomaly_flags`` — zero or more flags that travel with the block per
      ``docs/design/02-distillation-layer/external.md`` § 3.
    - ``regime_context`` — one-sentence label populated only when the regime
      is load-bearing for interpreting this block. ``None`` otherwise; the
      full regime payload is the universal broadcast (story 09), not
      duplicated into every block.
    """

    block_id: str
    audience: frozenset[OutputAudience]
    freshness_ts: datetime
    calibration_state: CalibrationState
    bootstrap_reason: str | None
    payload: Mapping[str, Any]
    anomaly_flags: tuple[AnomalyFlag, ...]
    regime_context: str | None

    def __post_init__(self) -> None:
        if not self.audience:
            raise ValueError(
                f"OutputBlock {self.block_id!r}: audience must declare at least one consumer"
            )


# ---------------------------------------------------------------------------
# Deterministic structured-text renderer
# ---------------------------------------------------------------------------
#
# Float-precision rules live as named constants so the convention is visible
# in one place rather than scattered through the formatter.

GENERAL_FLOAT_FORMAT = ".4g"
"""Format spec for general-purpose payload floats (``f"{x:.4g}"``)."""

PERCENTAGE_FLOAT_FORMAT = ".2f"
"""Format spec for percentage-style values such as anomaly magnitudes."""

COUNT_FLOAT_FORMAT = ".0f"
"""Format spec for share counts and other integer-valued floats.

Reserved for per-category stories whose payloads carry counts; centralized
here so future call sites use the named constant rather than inlining ``.0f``.
"""


_PAYLOAD_INDENT = "  "
_PER_TICKER_KEY = "per_ticker"


_NULL_SENTINEL = "null"
"""Operator-visible spelling for a payload field whose value is explicitly absent.

A payload value of ``None`` renders as ``null`` rather than the Python repr
``None`` — YAML-compatible so downstream tooling that parses block text
continues to recognize the sentinel.
"""


def _format_value(value: Any) -> str:
    """Render a leaf payload value, applying the float-precision rule for floats."""
    if value is None:
        return _NULL_SENTINEL
    if isinstance(value, float):
        return format(value, GENERAL_FLOAT_FORMAT)
    return str(value)


def _format_inline_value(value: Any) -> str:
    """Render a value inline for compact per-ticker rows.

    Lists and mappings serialize JSON-like (``[]``, ``{k:v,...}``) so a row
    stays on one line. Floats follow :data:`GENERAL_FLOAT_FORMAT`; nested
    mappings recurse via the same inline rule.
    """
    if value is None:
        return _NULL_SENTINEL
    if isinstance(value, float):
        return format(value, GENERAL_FLOAT_FORMAT)
    if isinstance(value, Mapping):
        items = ",".join(f"{k}:{_format_inline_value(value[k])}" for k in sorted(value))
        return "{" + items + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(_format_inline_value(v) for v in value) + "]"
    return str(value)


def _format_per_ticker(per_ticker: Mapping[str, Any]) -> list[str]:
    """Render the ``per_ticker`` mapping as one compact row per ticker.

    Each row is ``<TICKER> key1=val1 key2=val2 ...`` with sorted keys; the
    rendering compresses the per-ticker payloads that dominate sector-bundle
    size (ALP-272). Tickers without a Mapping value (defensive) fall back to
    inline-value rendering.
    """
    lines: list[str] = []
    for ticker in sorted(per_ticker):
        entry = per_ticker[ticker]
        if isinstance(entry, Mapping):
            fields = " ".join(f"{k}={_format_inline_value(entry[k])}" for k in sorted(entry))
            lines.append(f"{ticker} {fields}" if fields else ticker)
        else:
            lines.append(f"{ticker} {_format_inline_value(entry)}")
    return lines


def _format_payload(payload: Mapping[str, Any], depth: int = 0) -> list[str]:
    """Render a payload mapping as ``key: value`` lines, sorting keys.

    The ``per_ticker`` key (only at the top level) renders compactly: one row
    per ticker via :func:`_format_per_ticker` rather than a nested indented
    block — this is the dominant whitespace win for sector bundles.
    """
    lines: list[str] = []
    indent = _PAYLOAD_INDENT * depth
    for key in sorted(payload):
        value = payload[key]
        if depth == 0 and key == _PER_TICKER_KEY and isinstance(value, Mapping):
            lines.append(f"{key}:")
            lines.extend(_format_per_ticker(value))
        elif isinstance(value, Mapping):
            lines.append(f"{indent}{key}:")
            lines.extend(_format_payload(value, depth + 1))
        else:
            lines.append(f"{indent}{key}: {_format_value(value)}")
    return lines


def format_block(block: OutputBlock) -> str:
    """Render one :class:`OutputBlock` as deterministic structured text.

    The output shape follows
    ``docs/implementation/02-distillation-layer/05-output-envelope.md`` § Scope
    and is byte-identical on repeated calls — dictionaries iterate in
    sorted-key order, floats use the named precision constants above, and
    the current wall clock is never embedded. The "byte-identical on
    repeated calls" property is what makes the invocation archive diffable
    per ``docs/architecture/infrastructure.md`` § Invocation archive.

    Layout (compact per ALP-272): a single header line carries the block id,
    freshness, calibration tag, and optional regime context, separated by
    ``" | "``. The payload follows on the next line with no blank padding.
    The trailing ``Anomaly flags`` line is omitted when the block carries
    none — the universal anomaly summary already enumerates flags upstream.
    """
    header_parts = [
        f"### {block.block_id}",
        f"freshness {block.freshness_ts.isoformat()}",
    ]
    calibration_part = block.calibration_state.value
    if block.calibration_state is not CalibrationState.CALIBRATED:
        # The label is "reason" (not "bootstrap_reason") per ALP-540 — the
        # state itself names whether the reason is accumulating or
        # unavailable, so the prefix doesn't need to carry vocabulary.
        calibration_part += f" — reason: {block.bootstrap_reason}"
    header_parts.append(calibration_part)
    if block.regime_context is not None:
        header_parts.append(f"regime: {block.regime_context}")
    lines: list[str] = [" | ".join(header_parts)]
    lines.extend(_format_payload(block.payload))
    if block.anomaly_flags:
        lines.append(f"Anomaly flags ({len(block.anomaly_flags)}):")
        for flag in block.anomaly_flags:
            magnitude = format(flag.magnitude, PERCENTAGE_FLOAT_FORMAT)
            lines.append(f"  - {flag.name} | magnitude {magnitude} | severity {flag.severity}")
    return "\n".join(lines) + "\n"


def format_blocks_for_audience(
    blocks: Iterable[OutputBlock],
    audience: OutputAudience,
) -> str:
    """Filter blocks by audience membership and concatenate in deterministic order.

    Blocks are sorted ascending by ``block_id`` before rendering so the
    per-consumer assembly (story 11a/11b) sees a stable concatenation order
    independent of producer-side iteration. Returns the empty string when no
    blocks match.
    """
    matching = sorted(
        (block for block in blocks if audience in block.audience),
        key=lambda block: block.block_id,
    )
    return "".join(format_block(block) for block in matching)
