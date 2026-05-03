"""Input bundle assembler for the adaptive researcher — ALP-262.

Composes the user-message text the harness (story 05) sends to the adaptive
researcher: a header, the rendered volatility-regime block, the rendered
distillation-anomaly stream, and the rendered sector-researcher anomaly
stream. Pure function — no I/O, no logging, no LLM involvement.

Determinism is a hard invariant: two calls with the same inputs return
byte-equal ``bundle_text``.

Public names
------------
- :class:`InputBundle`
- :func:`assemble_input_bundle`
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from alphamind.analysis.adaptive_research.loaders import (
    AdaptiveAnomalyInputs,
    DistillationAnomalyRecord,
    SectorAnomalyRecord,
)

__all__ = [
    "InputBundle",
    "assemble_input_bundle",
]


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


class InputBundle(BaseModel, frozen=True):
    """Composed bundle as a value object; preserved for the diagnostic archive."""

    invocation_id: str
    as_of: datetime
    regime_text: str
    distillation_text: str
    sector_text: str
    bundle_text: str


# ---------------------------------------------------------------------------
# Public assembler
# ---------------------------------------------------------------------------


def assemble_input_bundle(
    *,
    invocation_id: str,
    as_of: datetime,
    regime_label: dict[str, Any],
    anomaly_inputs: AdaptiveAnomalyInputs,
) -> InputBundle:
    """Compose and return the user-message bundle.

    Pure function — no I/O, no logging, deterministic. The ``as_of`` timestamp
    is rendered as a UTC ISO 8601 string in the header; no other source of
    wall-clock time enters the rendered text.
    """
    header_text = _render_header(invocation_id=invocation_id, as_of=as_of)
    regime_text = _render_regime(regime_label)
    distillation_text = _render_distillation(anomaly_inputs.distillation)
    sector_text = _render_sector(anomaly_inputs.sector)

    bundle_text = _render_bundle(
        header_text=header_text,
        regime_text=regime_text,
        distillation_text=distillation_text,
        sector_text=sector_text,
    )

    return InputBundle(
        invocation_id=invocation_id,
        as_of=as_of,
        regime_text=regime_text,
        distillation_text=distillation_text,
        sector_text=sector_text,
        bundle_text=bundle_text,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_iso_utc(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC string (YYYY-MM-DDTHH:MM:SSZ)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_header(*, invocation_id: str, as_of: datetime) -> str:
    """Render the input header marker."""
    return (
        f"=== ADAPTIVE RESEARCH INPUT "
        f"(invocation {invocation_id}, as_of {_format_iso_utc(as_of)}) ==="
    )


_REGIME_FIELDS: tuple[tuple[str, str], ...] = (
    ("Regime", "regime"),
    ("Transition", "transition_flag"),
    ("Confidence", "confidence"),
    ("Freshness", "freshness_ts"),
)
_UNKNOWN = "unknown"


def _render_regime(regime_label: dict[str, Any]) -> str:
    """Render the VOLATILITY REGIME section.

    Reads each canonical key from ``regime_label`` with a default of
    ``unknown`` when missing — the upstream payload's exact shape is the
    contract; this renderer surfaces what's present without raising.
    """
    lines = ["=== VOLATILITY REGIME ==="]
    lines.extend(f"{label}: {regime_label.get(key, _UNKNOWN)}" for label, key in _REGIME_FIELDS)
    return "\n".join(lines)


def _render_distillation(records: tuple[DistillationAnomalyRecord, ...]) -> str:
    """Render the DISTILLATION ANOMALY FLAGS section."""
    header = f"=== DISTILLATION ANOMALY FLAGS ({len(records)} flags) ==="
    if not records:
        return f"{header}\n(none)"
    lines = [header]
    for i, rec in enumerate(records, start=1):
        regime_context = rec.regime_context if rec.regime_context is not None else "none"
        lines.append(
            f"[D-{i}] block={rec.block_id} flag={rec.flag_name}"
            f" magnitude={rec.magnitude:.2f} severity={rec.severity}"
        )
        lines.append(
            f"       freshness={_format_iso_utc(rec.freshness_ts)} regime_context={regime_context}"
        )
    return "\n".join(lines)


def _render_sector(records: tuple[SectorAnomalyRecord, ...]) -> str:
    """Render the SECTOR-RESEARCHER ANOMALIES section."""
    header = f"=== SECTOR-RESEARCHER ANOMALIES ({len(records)} anomalies) ==="
    if not records:
        return f"{header}\n(none)"
    lines = [header]
    for rec in records:
        ticker_csv = ", ".join(rec.tickers) if rec.tickers else "none"
        lines.append(
            f"[{rec.anomaly_id}] sector={rec.sector.value} type={rec.anomaly_type}"
            f" severity={rec.severity}"
        )
        lines.append(f"  Tickers: {ticker_csv}")
        lines.append(f"  Description: {rec.description}")
        lines.append(f"  Suggested question: {rec.suggested_question}")
    return "\n".join(lines)


def _render_bundle(
    *,
    header_text: str,
    regime_text: str,
    distillation_text: str,
    sector_text: str,
) -> str:
    """Join the four rendered sections with a blank line between sections.

    The final ``bundle_text`` ends with a single trailing newline.
    """
    return "\n\n".join([header_text, regime_text, distillation_text, sector_text]) + "\n"
