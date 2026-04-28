"""Cross-sector rotation classification with narrative tagging.

Implements ``compute_cross_sector_rotation``, ``_classify_velocity``,
``_classify_narrative``, and ``_compute_relative_strength_table``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise

from sqlalchemy.orm import Session

from alphamind.distillation.output import (
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _select_close_series,
    _window_bounds,
)

# ---------------------------------------------------------------------------
# Velocity and narrative constants
# ---------------------------------------------------------------------------

VELOCITY_SLOW = "slow_regime_shift"
"""Rotation classified as gradual cross-sector ranking change over multiple days."""

VELOCITY_SHARP = "sharp_intraday_event_driven"
"""Rotation classified as a single-session event moving multiple sectors past each other."""

ROTATION_NARRATIVE_RATE_DRIVEN = "rate_driven"
"""Financials vs. tech is the dominant cross-sector relative move."""

ROTATION_NARRATIVE_GROWTH_DRIVEN = "growth_driven"
"""Cyclicals vs. defensives (energy vs. tech) is the dominant relative move."""

ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN = "risk_appetite_driven"
"""High-beta vs. low-beta (small-cap vs. broad market) is the dominant relative move."""

# Single-session magnitude (in returns terms) above which a cross-sector move
# is treated as a sharp intraday event rather than a slow regime shift. Story
# 08d describes this discriminant qualitatively; numerically we treat any
# last-day move whose magnitude exceeds 4% as sharp. The constant is named so
# the rule is searchable and not a magic number scattered across the file.
_SHARP_INTRADAY_RETURN_THRESHOLD: float = 0.04

# Narrative-classification proxy pairs. Each entry maps a narrative label to
# a ``(numerator_etf, denominator_etf)`` tuple whose relative-strength move
# over the trailing short window represents the strength of that narrative.
# Per story 08d:
#   - rate_driven: financials (XLF) vs. tech (XLK)
#   - growth_driven: energy (XLE) vs. tech (XLK)  [cyclicals vs. defensives proxy]
#   - risk_appetite_driven: small-cap (IWM) vs. broad market (SPY)
_NARRATIVE_PAIRS: tuple[tuple[str, tuple[str, str]], ...] = (
    (ROTATION_NARRATIVE_RATE_DRIVEN, ("XLF", "XLK")),
    (ROTATION_NARRATIVE_GROWTH_DRIVEN, ("XLE", "XLK")),
    (ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN, ("IWM", "SPY")),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ratio_change(numer: list[float], denom: list[float]) -> float:
    """Return the percentage change of ``numer / denom`` between window edges.

    Window edges are the first and last entries of each series. Returns
    ``0.0`` when either edge is non-positive (a degenerate path the caller
    treats as no relative movement).
    """
    if not numer or not denom:
        return 0.0
    if numer[0] <= 0.0 or denom[0] <= 0.0:
        return 0.0
    if numer[-1] <= 0.0 or denom[-1] <= 0.0:
        return 0.0
    start = numer[0] / denom[0]
    end = numer[-1] / denom[-1]
    if start == 0.0:
        return 0.0
    return (end - start) / start


def _classify_velocity(
    *,
    closes_by_etf: dict[str, list[float]],
) -> str:
    """Classify rotation velocity as slow regime shift vs. sharp intraday event.

    Looks at the last single-day return for each ETF. If at least two ETFs
    move more than :data:`_SHARP_INTRADAY_RETURN_THRESHOLD` in opposite
    directions on the final session, the rotation is sharp; otherwise slow.
    """
    last_returns: list[float] = []
    for closes in closes_by_etf.values():
        # ``pairwise`` yields nothing when ``closes`` has fewer than two
        # elements; the last pair (if any) gives the latest day-over-day
        # return.
        last_pair: tuple[float, float] | None = None
        for prev, curr in pairwise(closes):
            last_pair = (prev, curr)
        if last_pair is None:
            continue
        prev, curr = last_pair
        if prev <= 0.0:
            continue
        last_returns.append((curr - prev) / prev)
    if not last_returns:
        return VELOCITY_SLOW
    big_up = any(r > _SHARP_INTRADAY_RETURN_THRESHOLD for r in last_returns)
    big_down = any(r < -_SHARP_INTRADAY_RETURN_THRESHOLD for r in last_returns)
    if big_up and big_down:
        return VELOCITY_SHARP
    return VELOCITY_SLOW


def _classify_narrative(
    *,
    closes_by_etf: dict[str, list[float]],
) -> str:
    """Tag the dominant rotation narrative.

    Returns the narrative whose proxy-pair relative-strength move (absolute
    magnitude) is largest over the available window. A pair whose tickers are
    missing from ``closes_by_etf`` contributes magnitude ``0`` (i.e. cannot
    win); when no pair has data, returns the first label so the output block
    still carries a deterministic value.
    """
    best_label = _NARRATIVE_PAIRS[0][0]
    best_magnitude = -1.0
    for label, (numer_ticker, denom_ticker) in _NARRATIVE_PAIRS:
        numer = closes_by_etf.get(numer_ticker, [])
        denom = closes_by_etf.get(denom_ticker, [])
        change = abs(_ratio_change(numer, denom))
        if change > best_magnitude:
            best_magnitude = change
            best_label = label
    return best_label


def _compute_relative_strength_table(
    closes_by_etf: dict[str, list[float]],
) -> dict[str, dict[str, float]]:
    """Per-ETF percentage move over the loaded window.

    Result keys are sorted ETF tickers; each value is ``{"return": <pct>}``.
    """
    table: dict[str, dict[str, float]] = {}
    for ticker in sorted(closes_by_etf):
        closes = closes_by_etf[ticker]
        # ``pairwise`` is empty when ``closes`` has fewer than two entries;
        # treat that as zero relative move so the block still emits.
        if not list(pairwise(closes)) or closes[0] <= 0.0:
            table[ticker] = {"return": 0.0}
            continue
        ret = (closes[-1] - closes[0]) / closes[0]
        table[ticker] = {"return": ret}
    return table


# ---------------------------------------------------------------------------
# Public compute function
# ---------------------------------------------------------------------------


def compute_cross_sector_rotation(
    session: Session,
    *,
    sector_etfs: Sequence[str],
    risk_proxies: Sequence[str],
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
) -> list[OutputBlock]:
    """Compute the cross-sector rotation block.

    For ``sector_etfs`` plus ``risk_proxies`` returns one
    :class:`OutputBlock` whose payload carries:

    - ``short_window.relative_performance`` / ``long_window.relative_performance``
      — per-ETF percentage move over each window (sorted by ticker).
    - ``velocity_label`` — :data:`VELOCITY_SLOW` or :data:`VELOCITY_SHARP`.
    - ``narrative_label`` — :data:`ROTATION_NARRATIVE_RATE_DRIVEN`,
      :data:`ROTATION_NARRATIVE_GROWTH_DRIVEN`, or
      :data:`ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN`.

    The block routes to :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.
    """
    long_start, range_end = _window_bounds(as_of=as_of, window_days=long_window_days)

    all_etfs = tuple(sector_etfs) + tuple(risk_proxies)
    short_closes: dict[str, list[float]] = {}
    long_closes: dict[str, list[float]] = {}
    for etf in all_etfs:
        # One query per ETF: read the long-window series and slice the
        # short window from its tail. Avoids redundant range queries.
        long_closes[etf] = _select_close_series(
            session, ticker=etf, range_start=long_start, range_end=range_end
        )
        short_closes[etf] = long_closes[etf][-short_window_days:]

    velocity = _classify_velocity(closes_by_etf=short_closes)
    narrative = _classify_narrative(closes_by_etf=short_closes)

    short_table = _compute_relative_strength_table(short_closes)
    long_table = _compute_relative_strength_table(long_closes)

    n_observed = min(
        (len(closes) for closes in short_closes.values() if closes),
        default=0,
    )
    state, reason = _calibration_for_window(
        n_observations=n_observed,
        required=short_window_days,
        input_name="cross_sector_short_window_observations",
    )

    payload = {
        "short_window": {
            "relative_performance": short_table,
            "window_days": short_window_days,
        },
        "long_window": {
            "relative_performance": long_table,
            "window_days": long_window_days,
        },
        "velocity_label": velocity,
        "narrative_label": narrative,
    }
    block = OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.cross_sector_rotation",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=(),
        regime_context=None,
    )
    return [block]
