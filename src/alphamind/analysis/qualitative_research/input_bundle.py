"""Input bundle assembler for the qualitative researcher — ALP-251.

Composes the user-message text the harness sends to the qualitative
researcher: a header, the rendered regime block, the news digest text,
and rendered sections for each of the four loader outputs.  Output is a
single deterministic string the LLM reads as its full input.

Pure function — no I/O, no logging, no LLM involvement.

Public names
------------
- :class:`InputBundle`
- :func:`assemble_input_bundle`
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from alphamind._kernel.calibration import CalibrationState
from alphamind.analysis.qualitative_research.loaders import (
    QualitativeInputs,
    SentimentAggregate,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest

__all__ = [
    "InputBundle",
    "assemble_input_bundle",
]


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InputBundle:
    """The composed bundle as a value object; also used for diagnostic preservation."""

    invocation_id: str
    as_of: datetime
    regime_text: str
    digest_text: str
    sentiment_text: str
    prediction_market_text: str
    calendar_text: str
    thesis_text: str
    bundle_text: str


# ---------------------------------------------------------------------------
# Public assembler
# ---------------------------------------------------------------------------


def assemble_input_bundle(
    *,
    invocation_id: str,
    as_of: datetime,
    regime_label: dict[str, Any],
    digest: NewsDigest,
    inputs: QualitativeInputs,
) -> InputBundle:
    """Compose and return the full user-message bundle for the qualitative researcher.

    Pure function — no I/O, no logging, deterministic.
    """
    regime_text = _render_regime(regime_label)
    digest_text = digest.digest_text
    sentiment_text = _render_sentiment(inputs)
    prediction_market_text = _render_prediction_markets(inputs)
    calendar_text = _render_calendar(inputs)
    thesis_text = _render_theses(inputs)

    bundle_text = _render_bundle(
        invocation_id=invocation_id,
        as_of=as_of,
        regime_text=regime_text,
        digest_text=digest_text,
        sentiment_text=sentiment_text,
        prediction_market_text=prediction_market_text,
        calendar_text=calendar_text,
        thesis_text=thesis_text,
    )

    return InputBundle(
        invocation_id=invocation_id,
        as_of=as_of,
        regime_text=regime_text,
        digest_text=digest_text,
        sentiment_text=sentiment_text,
        prediction_market_text=prediction_market_text,
        calendar_text=calendar_text,
        thesis_text=thesis_text,
        bundle_text=bundle_text,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_iso_utc(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC string (YYYY-MM-DDTHH:MM:SSZ)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_regime(regime_label: dict[str, Any]) -> str:
    """Render the VOLATILITY REGIME section body.

    Renders every key in ``regime_label`` as a ``key: value`` line in
    deterministic alphabetical key order. The bundle is payload-agnostic:
    additions to the regime payload (currently seven supporting indicators
    on top of the four labels per :func:`alphamind.distillation.regime
    .assemble_regime_block`) propagate to the LLM without renderer edits.
    """
    return "\n".join(f"{key}: {regime_label[key]}" for key in sorted(regime_label))


def _render_sentiment(inputs: QualitativeInputs) -> str:
    """Render per-ticker sentiment aggregates sorted alphabetically by ticker.

    Non-calibrated rows (``UNAVAILABLE`` or ``ACCUMULATING``) surface as a
    compact ``{ticker}: {state}`` line so per-ticker missing data stays
    distinguishable from per-field ``null`` fallbacks inside otherwise-numeric
    rows. Both non-calibrated states emit all-``None`` numeric fields per
    ALP-568 / ALP-538; the collapsed line preserves the operator-visible
    difference between "collector dead" and "still accumulating".
    """
    rows = sorted(inputs.sentiment_aggregates, key=lambda s: s.ticker)
    return "\n".join(_render_sentiment_row(s) for s in rows)


def _render_sentiment_row(s: SentimentAggregate) -> str:
    """Render one sentiment row, dispatching on ``calibration_state``."""
    if s.calibration_state is not CalibrationState.CALIBRATED:
        return f"{s.ticker}: {s.calibration_state.value}"
    return (
        f"{s.ticker}: directional={_render_optional(s.directional_score)},"
        f" magnitude={_render_optional(s.magnitude)},"
        f" change={_render_optional(s.rate_of_change)},"
        f" vol={_render_optional(s.volume)},"
        f" percentile={_render_optional(s.percentile_vs_self)},"
        f" divergence={_render_optional(s.divergence_flag)}"
    )


def _render_optional(value: object) -> str:
    """Render ``None`` as ``null``; otherwise stringify the value."""
    return "null" if value is None else str(value)


def _render_prediction_markets(inputs: QualitativeInputs) -> str:
    """Render prediction-market snapshot rows.

    Active rows sort alphabetically by contract_id first; stale-low-signal
    rows (likely-resolved-but-not-yet-closed; ALP-536) sort to the bottom,
    alphabetical within that group, and carry a ``[STALE — LIKELY RESOLVED]``
    flag so downstream agents read them as deprioritized.
    """
    rows = sorted(inputs.prediction_markets, key=lambda p: (p.is_stale_low_signal, p.contract_id))
    lines = []
    for p in rows:
        vol_str = f"${p.volume_24h_usd}" if p.volume_24h_usd is not None else "$0"
        line = (
            f"[{p.contract_id}] {p.description} ({p.platform}/{p.category}):"
            f" prob={p.current_probability},"
            f" Δ_invocation={p.delta_since_last_invocation_pp}pp,"
            f" Δ_since_prior={p.delta_since_prior_pp}pp,"
            f" vol_24h={vol_str},"
            f" expires={p.expiration}"
        )
        if p.meets_threshold_flag:
            line += " [FLAGGED]"
        if p.is_low_liquidity:
            line += " [LOW LIQUIDITY]"
        if p.is_stale_low_signal:
            line += " [STALE — LIKELY RESOLVED]"
        lines.append(line)
    return "\n".join(lines)


def _render_calendar(inputs: QualitativeInputs) -> str:
    """Render calendar events sorted by event_time ascending."""
    rows = sorted(inputs.events, key=lambda e: e.event_time)
    lines = []
    for e in rows:
        sectors_str = ", ".join(sorted(s.value for s in e.sectors)) if e.sectors else "(none)"
        tickers_str = ", ".join(e.tickers) if e.tickers else "(none)"
        consensus_str = e.consensus if e.consensus is not None else "(none)"
        lines.append(
            f"- {_format_iso_utc(e.event_time)} | {e.event_name}"
            f" | type={e.event_type}"
            f" | sectors={sectors_str}"
            f" | tickers={tickers_str}"
            f" | consensus={consensus_str}"
        )
    return "\n".join(lines)


_NO_ACTIVE_THESES = "(no active theses)."


def _render_theses(inputs: QualitativeInputs) -> str:
    """Render active thesis summaries sorted by thesis_id."""
    if not inputs.theses:
        return _NO_ACTIVE_THESES
    rows = sorted(inputs.theses, key=lambda t: t.thesis_id)
    return "\n".join(
        f"[{t.thesis_id}] {t.ticker}: {t.summary}"
        f" | catalyst: {t.key_catalyst}"
        f" | time: {t.time_expectation_hours}h"
        for t in rows
    )


def _render_bundle(
    *,
    invocation_id: str,
    as_of: datetime,
    regime_text: str,
    digest_text: str,
    sentiment_text: str,
    prediction_market_text: str,
    calendar_text: str,
    thesis_text: str,
) -> str:
    """Assemble the full bundle text from its rendered parts."""
    parts: list[str] = [
        "# QUALITATIVE RESEARCHER INPUT BUNDLE",
        f"Invocation: {invocation_id}",
        f"As of: {_format_iso_utc(as_of)}",
        "",
        "## VOLATILITY REGIME",
        "",
        regime_text,
        "",
        "## NEWS DIGEST",
        "",
        digest_text,
        "",
        "## SENTIMENT AGGREGATES",
        "",
        sentiment_text,
        "",
        "## PREDICTION-MARKET SNAPSHOT",
        "",
        prediction_market_text,
        "",
        "## EVENT CALENDAR (next 72h)",
        "",
        calendar_text,
        "",
        "## ACTIVE THESIS SUMMARIES",
        "",
        thesis_text,
        "",
    ]
    return "\n".join(parts)
