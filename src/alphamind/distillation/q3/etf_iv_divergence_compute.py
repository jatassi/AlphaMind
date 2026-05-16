"""Pure-compute core for the q3 ETF/single-name IV divergence (ALP-484).

The divergence detection is already pure — it consumes a per-sector
``Mapping`` of pre-loaded IV inputs and emits one
:class:`EtfIvDivergence` per sector whose spread z-score clears the
threshold. The IO shell that builds the input mapping lives in
:mod:`.etf_iv_divergence_loaders`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

EtfIvDivergenceDirection = Literal["etf_leading_names", "names_leading_etf"]
"""Direction taxonomy for the ETF-vs-single-name IV divergence.

Per ``external.md`` § 2: "Direction matters: ETF leading the names is a
'sector view hasn't propagated' signal; names leading the ETF is the
inverse."
"""


@dataclass(frozen=True, slots=True)
class EtfIvDivergence:
    """One per-sector divergence detection."""

    sector: str
    etf_ticker: str
    etf_iv: float
    single_name_aggregate_iv: float
    spread: float
    spread_zscore: float
    direction: EtfIvDivergenceDirection


@dataclass(frozen=True, slots=True)
class EtfIvDivergenceInputs:
    """Frozen inputs for :func:`compute_etf_iv_divergences`.

    Each ``sectors[sector]`` mapping carries:

    * ``etf_ticker`` (str) — the sector ETF symbol.
    * ``etf_iv`` (float) — the ETF's ATM-call IV at ``as_of``.
    * ``single_name_aggregate_iv`` (float) — the volume-weighted
      aggregate single-name ATM-call IV at ``as_of``.
    * ``spread_baseline_mean`` (float) — trailing-window mean of the
      ``etf_iv - single_name_iv`` spread.
    * ``spread_baseline_stdev`` (float) — trailing-window stdev of the
      same spread.
    """

    sectors: Mapping[str, Mapping[str, float | str]]


def compute_etf_iv_divergences(
    *,
    sectors: Mapping[str, Mapping[str, float | str]],
    sigma_threshold: float,
) -> list[EtfIvDivergence]:
    """Detect ETF-IV vs single-name-IV divergences across sectors.

    Each ``sectors[sector]`` mapping carries ``etf_ticker`` (str), ``etf_iv``,
    ``single_name_aggregate_iv``, ``spread_baseline_mean``, and
    ``spread_baseline_stdev``. The spread is ``etf_iv -
    single_name_aggregate_iv``; the divergence z-score is ``(spread -
    baseline_mean) / baseline_stdev``. A divergence fires when
    ``abs(z) >= sigma_threshold``.

    The direction tag is assigned by the sign of the z-score: positive
    z reads as ``etf_leading_names`` (ETF IV moved further than the spread
    baseline), negative as ``names_leading_etf``.

    Sectors with a non-positive baseline stdev are skipped — z-score is
    undefined and the caller is expected to wait for calibrated baseline.
    """
    divergences: list[EtfIvDivergence] = []
    for sector in sorted(sectors):
        payload = sectors[sector]
        etf_iv = float(payload["etf_iv"])
        single_name_iv = float(payload["single_name_aggregate_iv"])
        baseline_mean = float(payload["spread_baseline_mean"])
        baseline_stdev = float(payload["spread_baseline_stdev"])
        etf_ticker = str(payload["etf_ticker"])
        if baseline_stdev <= 0:
            continue
        spread = etf_iv - single_name_iv
        zscore = (spread - baseline_mean) / baseline_stdev
        if abs(zscore) < sigma_threshold:
            continue
        direction: EtfIvDivergenceDirection = (
            "etf_leading_names" if zscore > 0 else "names_leading_etf"
        )
        divergences.append(
            EtfIvDivergence(
                sector=sector,
                etf_ticker=etf_ticker,
                etf_iv=etf_iv,
                single_name_aggregate_iv=single_name_iv,
                spread=spread,
                spread_zscore=zscore,
                direction=direction,
            )
        )
    return divergences


__all__ = [
    "EtfIvDivergence",
    "EtfIvDivergenceDirection",
    "EtfIvDivergenceInputs",
    "compute_etf_iv_divergences",
]
