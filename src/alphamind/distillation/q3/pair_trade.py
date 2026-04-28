"""Q3 pair-trade signature detection — quant 3g.

Implements:
- :class:`FlowZScore` — per-ticker z-scored BTO flow magnitudes.
- :class:`PairTradeSignature` — one detected cross-ticker pair-trade signal.
- :func:`detect_pair_trade_signatures` — scan correlated pairs for
  simultaneous call-BTO / put-BTO spikes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FlowZScore:
    """Per-ticker z-scored opening-flow magnitudes for the current window.

    The two halves split BTO flow by direction so the pair detector can ask
    "which direction was the unusual flow on this name?" The orchestrator
    computes z-scores against per-ticker rolling baselines (story 07's
    refresh primitive) and passes them in.
    """

    call_bto_z: float
    put_bto_z: float


@dataclass(frozen=True, slots=True)
class PairTradeSignature:
    """One detected pair-trade signature.

    The bullish-leg ticker carries the call-BTO spike; the bearish-leg
    ticker carries the put-BTO spike. The pair is correlated on a 60-day
    return basis at the level captured in ``correlation``.
    """

    bullish_leg: str
    bearish_leg: str
    bullish_call_bto_z: float
    bearish_put_bto_z: float
    correlation: float


def detect_pair_trade_signatures(
    *,
    flow_zscores: Mapping[str, FlowZScore],
    pair_correlations: Mapping[tuple[str, str], float],
    sigma_threshold: float,
    correlation_threshold: float,
) -> list[PairTradeSignature]:
    """Scan ``pair_correlations`` for pair-trade signatures.

    For each ``(a, b)`` pair whose absolute correlation is at least
    ``correlation_threshold``: emit a ``PairTradeSignature`` when one leg's
    ``call_bto_z`` is above ``sigma_threshold`` AND the other's ``put_bto_z``
    is above ``sigma_threshold``. The bullish/bearish role assignment falls
    out of the direction tags — the leg with the call spike is bullish.

    Pairs missing from ``flow_zscores`` are skipped — no flow to score.
    Symmetric ordering: ``(NVDA, AMD)`` and ``(AMD, NVDA)`` are detected
    once each, since the call-leg vs put-leg roles are direction-asymmetric.
    """
    signatures: list[PairTradeSignature] = []
    for (a, b), correlation in pair_correlations.items():
        if abs(correlation) < correlation_threshold:
            continue
        a_score = flow_zscores.get(a)
        b_score = flow_zscores.get(b)
        if a_score is None or b_score is None:
            continue
        if a_score.call_bto_z >= sigma_threshold and b_score.put_bto_z >= sigma_threshold:
            signatures.append(
                PairTradeSignature(
                    bullish_leg=a,
                    bearish_leg=b,
                    bullish_call_bto_z=a_score.call_bto_z,
                    bearish_put_bto_z=b_score.put_bto_z,
                    correlation=correlation,
                )
            )
    return signatures
