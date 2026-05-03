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

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from alphamind.analysis.qualitative_research.loaders import (
    QualitativeInputs,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest

__all__ = [
    "InputBundle",
    "assemble_input_bundle",
]


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


class InputBundle(BaseModel, frozen=True):
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
    """Render the VOLATILITY REGIME section body."""
    label = regime_label.get("regime_label", "")
    transition = regime_label.get("transition_state", "")
    prior = regime_label.get("prior_label")
    held = regime_label.get("invocations_held", 0)
    prior_str = str(prior) if prior is not None else "(none)"
    return (
        f"regime_label: {label}\n"
        f"transition_state: {transition}\n"
        f"prior_label: {prior_str}\n"
        f"invocations_held: {held}"
    )


def _render_sentiment(inputs: QualitativeInputs) -> str:
    """Render per-ticker sentiment aggregates sorted alphabetically by ticker."""
    rows = sorted(inputs.sentiment_aggregates, key=lambda s: s.ticker)
    return "\n".join(
        f"{s.ticker}: directional={s.directional_score},"
        f" magnitude={s.magnitude},"
        f" change={s.rate_of_change},"
        f" vol={s.volume},"
        f" percentile={s.percentile_vs_self},"
        f" divergence={s.divergence_flag}"
        for s in rows
    )


def _render_prediction_markets(inputs: QualitativeInputs) -> str:
    """Render prediction-market snapshot rows sorted by contract_id."""
    rows = sorted(inputs.prediction_markets, key=lambda p: p.contract_id)
    lines = []
    for p in rows:
        vol_str = f"${p.volume_24h_usd}" if p.volume_24h_usd is not None else "$0"
        line = (
            f"[{p.contract_id}] {p.description} ({p.platform}/{p.category}):"
            f" prob={p.current_probability},"
            f" Δ_invocation={p.delta_since_last_invocation_pp}pp,"
            f" Δ_24h={p.delta_24h_pp}pp,"
            f" vol_24h={vol_str},"
            f" expires={p.expiration}"
        )
        if p.meets_threshold_flag:
            line += " [FLAGGED]"
        if p.is_low_liquidity:
            line += " [LOW LIQUIDITY]"
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


_THESES_STUB = "(no active theses — execution-layer thesis model pending per ALP-111)."


def _render_theses(inputs: QualitativeInputs) -> str:
    """Render active thesis summaries sorted by thesis_id."""
    if not inputs.theses:
        return _THESES_STUB
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
