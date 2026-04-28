"""Multi-timeframe divergence-flag detection — story 02-distillation/08a.

A divergence fires when an indicator at one timeframe disagrees with the
same indicator at the next-higher timeframe — for example, 4h RSI
bearish (< 30) while daily RSI is healthy (> 50). External.md calls these
"among the most actionable outputs for sector researchers" so the rollup
field lists every firing pair.

The five timeframes are the same set used elsewhere in the layer:
``15min`` < ``1h`` < ``4h`` < ``1d`` < ``1w``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from alphamind.distillation.q1.indicators import RsiResult

# Conventional RSI bands: oversold below 30, overbought above 70, healthy
# is the wide neutral region centered on 50. Wilder's original definition
# fixes these values; they are math constants of the RSI indicator, not
# Class A thresholds.
_RSI_OVERSOLD = 30.0
_RSI_OVERBOUGHT = 70.0
_RSI_HEALTHY_LOWER = 40.0
_RSI_HEALTHY_UPPER = 60.0


# Adjacent-timeframe ordering: each tuple is ``(lower, higher)``. The
# divergence detector compares the indicator at the lower timeframe to the
# indicator at the higher timeframe.
_TIMEFRAME_PAIRS: tuple[tuple[str, str], ...] = (
    ("15min", "1h"),
    ("1h", "4h"),
    ("4h", "1d"),
    ("1d", "1w"),
)

DivergenceKind = Literal["rsi"]


@dataclass(frozen=True, slots=True)
class DivergenceFlag:
    """One divergence between paired adjacent timeframes.

    ``kind`` names the underlying indicator (currently ``"rsi"``);
    ``lower_timeframe`` and ``higher_timeframe`` are the adjacent pair the
    flag was raised on; ``description`` is the short human-readable text
    surfaced in the divergence-flags rollup payload.
    """

    kind: DivergenceKind
    lower_timeframe: str
    higher_timeframe: str
    description: str


def _classify_rsi_band(rsi: float) -> str:
    """Bucket an RSI reading into oversold / healthy / overbought / mid.

    The "mid" buckets between healthy-and-oversold and healthy-and-overbought
    do not raise divergence flags on their own; they only matter when paired
    with a clearly-bucketed counterpart.
    """
    if rsi <= _RSI_OVERSOLD:
        return "oversold"
    if rsi >= _RSI_OVERBOUGHT:
        return "overbought"
    if _RSI_HEALTHY_LOWER <= rsi <= _RSI_HEALTHY_UPPER:
        return "healthy"
    return "mid"


def _is_divergent(lower_band: str, higher_band: str) -> bool:
    """Return ``True`` when the two band classifications conflict.

    The divergence rule from
    ``docs/implementation/02-distillation-layer/08a-q1-price-volume-indicators.md``:
    a lower-timeframe oversold or overbought reading conflicts with a
    higher-timeframe healthy reading. Symmetrically, two opposite extreme
    readings (oversold on one, overbought on the other) also count.
    """
    extremes = {"oversold", "overbought"}
    if lower_band in extremes and higher_band == "healthy":
        return True
    if lower_band == "healthy" and higher_band in extremes:
        return True
    return lower_band in extremes and higher_band in extremes and lower_band != higher_band


def _format_rsi_description(
    *,
    lower_tf: str,
    higher_tf: str,
    lower_rsi: float,
    higher_rsi: float,
) -> str:
    return (
        f"RSI {lower_tf} {lower_rsi:.1f} vs {higher_tf} {higher_rsi:.1f}: "
        f"{_classify_rsi_band(lower_rsi)} vs {_classify_rsi_band(higher_rsi)}"
    )


def detect_rsi_divergences(
    rsi_per_timeframe: Mapping[str, RsiResult],
) -> tuple[DivergenceFlag, ...]:
    """Return one :class:`DivergenceFlag` per firing adjacent-timeframe pair.

    The flag fires when the lower-timeframe RSI is in an extreme band
    (oversold at-or-below 30, or overbought at-or-above 70) while the
    higher timeframe is healthy (40 to 60), or when the two extremes are
    opposite.

    Returns an empty tuple when no pair fires. Pairs whose timeframes are
    missing from ``rsi_per_timeframe`` are silently skipped — the caller
    decides whether a missing timeframe is a data error.
    """
    flags: list[DivergenceFlag] = []
    for lower_tf, higher_tf in _TIMEFRAME_PAIRS:
        lower = rsi_per_timeframe.get(lower_tf)
        higher = rsi_per_timeframe.get(higher_tf)
        if lower is None or higher is None:
            continue
        lower_band = _classify_rsi_band(lower.value)
        higher_band = _classify_rsi_band(higher.value)
        if not _is_divergent(lower_band, higher_band):
            continue
        flags.append(
            DivergenceFlag(
                kind="rsi",
                lower_timeframe=lower_tf,
                higher_timeframe=higher_tf,
                description=_format_rsi_description(
                    lower_tf=lower_tf,
                    higher_tf=higher_tf,
                    lower_rsi=lower.value,
                    higher_rsi=higher.value,
                ),
            )
        )
    return tuple(flags)


__all__ = [
    "DivergenceFlag",
    "DivergenceKind",
    "detect_rsi_divergences",
]
