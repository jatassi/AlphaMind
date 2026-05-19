"""Q6 OutputBlock assembly — ``q6.*`` namespace (ALP-485).

ALP-485 split this module along the compute/load boundary:

* :func:`assemble_q6_blocks_from_inputs` — pure compute over a frozen
  :class:`alphamind.distillation.q6._loaders.Q6Inputs`. The orchestrator's
  Phase 2 calls this under ``asyncio.TaskGroup`` + ``asyncio.to_thread``
  in parallel with q1 / q3 / qualitative.
* :func:`compute_q6_blocks` — thin session-accepting shim that wraps the
  session in :func:`load_q6_inputs` and delegates to the pure compute.

The per-block-type builders (``_build_yield_curve_block`` etc.) live here
as before; they are pure functions over their respective frozen result
types. The session-bound composite-refresh writes that flush the Session
(funding-stress + market-liquidity) execute inside the loader so the
parallel pure compute cannot conflict with another category's flush.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q6._loaders import Q6Inputs, load_q6_inputs
from alphamind.distillation.q6.dollar_attribution_compute import DollarAttributionResult
from alphamind.distillation.q6.funding_stress_compute import FundingStressResult
from alphamind.distillation.q6.inflation_compute import InflationRegimeResult
from alphamind.distillation.q6.market_liquidity_compute import MarketLiquidityResult
from alphamind.distillation.q6.yield_curve_compute import YieldCurveRegimeResult

# ---------------------------------------------------------------------------
# Output assembly — every Q6 block is UNIVERSAL_BROADCAST
# ---------------------------------------------------------------------------
#
# Per ``external.md`` § 4 Persistent state and composites and the analysis-
# layer agent contract, macro context is not sector-scoped: every analyst
# reads it, the synthesizer reads it via the correlation/regime brief, and
# the strategist reads it through its input bundle. UNIVERSAL_BROADCAST
# encodes that audience choice once per block.

UNIVERSAL_BROADCAST_AUDIENCE: frozenset[OutputAudience] = frozenset(
    {OutputAudience.UNIVERSAL_BROADCAST}
)


def _build_yield_curve_block(
    result: YieldCurveRegimeResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id="q6.yield_curve_regime",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "label": result.label.value,
            "spread_2s10s": result.spread_2s10s,
            "spread_3m10y": result.spread_3m10y,
            "spread_5s30s": result.spread_5s30s,
            "regime_transition": result.regime_transition,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _build_inflation_block(
    result: InflationRegimeResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id="q6.inflation_regime",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "label": result.label.value,
            "breakeven_trend_bps": result.breakeven_trend_bps,
            "cpi_surprise_signs_positive": result.cpi_surprise_signs_positive,
            "cpi_surprise_signs_negative": result.cpi_surprise_signs_negative,
            "regime_transition": result.regime_transition,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _build_dollar_attribution_block(
    result: DollarAttributionResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id="q6.dollar_attribution",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "label": result.label.value,
            "rate_correlation": result.rate_correlation,
            "risk_correlation": result.risk_correlation,
        },
        anomaly_flags=(),
        regime_context=None,
    )


def _build_funding_stress_block(
    result: FundingStressResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    flags: tuple[AnomalyFlag, ...] = ()
    if result.alert_active:
        # Magnitude carries the count of components above the alert
        # percentile, so a downstream consumer can read severity proxy
        # without re-deriving it. Severity is ``investigate_now`` per
        # ``external.md`` § 3 Anomaly detection (Macro).
        flags = (
            AnomalyFlag(
                name="funding_stress_alert",
                magnitude=float(result.components_above_percentile),
                severity="investigate_now",
            ),
        )
    return OutputBlock(
        block_id="q6.funding_stress",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=result.state,
        bootstrap_reason=result.bootstrap_reason,
        payload={
            "composite_value": result.composite_value,
            "components": dict(result.components),
            "component_percentiles": dict(result.component_percentiles),
            "components_above_percentile": result.components_above_percentile,
            "alert_active": result.alert_active,
        },
        anomaly_flags=flags,
        regime_context=None,
    )


def _build_market_liquidity_block(
    result: MarketLiquidityResult,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    flags: tuple[AnomalyFlag, ...] = ()
    # alert_active can only fire on a defined percentile (per ALP-545's
    # _alert_active null branch); the explicit None guard pins the
    # invariant for the type checker.
    if result.alert_active and result.percentile_60d is not None:
        flags = (
            AnomalyFlag(
                name="market_liquidity_alert",
                magnitude=float(result.percentile_60d),
                severity="investigate_now",
            ),
        )
    return OutputBlock(
        block_id="q6.market_liquidity",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=result.state,
        bootstrap_reason=result.bootstrap_reason,
        payload={
            "composite_value": result.composite_value,
            "components": dict(result.components),
            "percentile_60d": result.percentile_60d,
            "alert_active": result.alert_active,
        },
        anomaly_flags=flags,
        regime_context=None,
    )


def _build_macro_surprise_anomaly_block(
    indicator: str,
    flag: AnomalyFlag,
    *,
    freshness_ts: datetime,
) -> OutputBlock:
    return OutputBlock(
        block_id=f"q6.macro_surprise_anomaly.{indicator}",
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={
            "indicator": indicator,
            "z_score": flag.magnitude,
        },
        anomaly_flags=(flag,),
        regime_context=None,
    )


def _build_unavailable_block(
    *,
    block_id: str,
    bootstrap_reason: str,
    freshness_ts: datetime,
) -> OutputBlock:
    """Build a stand-in q6 block when an essential input series is missing.

    Used for the yield-curve / inflation / dollar paths whose existing
    ``_build_*_block`` helpers hardcode ``CalibrationState.CALIBRATED``.
    The block carries :attr:`CalibrationState.UNAVAILABLE` per ALP-540 —
    missing FRED/macro inputs are collector failures requiring operator
    action, not "wait for more data." Empty payload + no anomaly flags so
    a downstream consumer reads the calibration tag (and the
    ``bootstrap_reason`` it carries) rather than a fabricated label.
    """
    return OutputBlock(
        block_id=block_id,
        audience=UNIVERSAL_BROADCAST_AUDIENCE,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.UNAVAILABLE,
        bootstrap_reason=bootstrap_reason,
        payload={},
        anomaly_flags=(),
        regime_context=None,
    )


def assemble_q6_blocks(
    *,
    yield_curve: YieldCurveRegimeResult,
    inflation: InflationRegimeResult,
    dollar_attribution: DollarAttributionResult,
    funding_stress: FundingStressResult,
    market_liquidity: MarketLiquidityResult,
    macro_surprise_anomalies: Sequence[tuple[str, AnomalyFlag]],
    freshness_ts: datetime,
) -> tuple[OutputBlock, ...]:
    """Pack the Q6 computations into ``OutputBlock`` instances.

    Every block carries ``audience = UNIVERSAL_BROADCAST`` per the story
    scope: macro context is not sector-scoped — analyst, synthesizer, and
    strategist all read it.

    The macro surprise anomalies are emitted as one block per detection
    (block_id ``q6.macro_surprise_anomaly.<indicator>``) so consumers can
    address them individually; the other five blocks are singletons keyed
    by their fixed ``block_id``.
    """
    blocks: list[OutputBlock] = [
        _build_yield_curve_block(yield_curve, freshness_ts=freshness_ts),
        _build_inflation_block(inflation, freshness_ts=freshness_ts),
        _build_dollar_attribution_block(dollar_attribution, freshness_ts=freshness_ts),
        _build_funding_stress_block(funding_stress, freshness_ts=freshness_ts),
        _build_market_liquidity_block(market_liquidity, freshness_ts=freshness_ts),
    ]
    for indicator, flag in macro_surprise_anomalies:
        blocks.append(
            _build_macro_surprise_anomaly_block(indicator, flag, freshness_ts=freshness_ts)
        )
    return tuple(blocks)


def assemble_q6_blocks_from_inputs(inputs: Q6Inputs) -> list[OutputBlock]:
    """Pure-compute assembly of every Q6 :class:`OutputBlock`.

    Operates entirely on the pre-loaded :class:`Q6Inputs`; no DB access.
    This is the function the orchestrator's Phase 2 calls under
    ``asyncio.TaskGroup`` + ``asyncio.to_thread`` parallel with q1 / q3 /
    qualitative.

    When all three label-input classifiers (yield-curve, inflation,
    dollar) produced a result, the calibrated path runs through
    :func:`assemble_q6_blocks`. Missing label-inputs route through
    :func:`_build_unavailable_block` so the block_id is preserved but
    the calibration tag accurately reports the gap.
    """
    if (
        inputs.yield_curve is not None
        and inputs.inflation is not None
        and inputs.dollar_attribution is not None
    ):
        return list(
            assemble_q6_blocks(
                yield_curve=inputs.yield_curve,
                inflation=inputs.inflation,
                dollar_attribution=inputs.dollar_attribution,
                funding_stress=inputs.funding_stress,
                market_liquidity=inputs.market_liquidity,
                macro_surprise_anomalies=inputs.macro_surprise_anomalies,
                freshness_ts=inputs.as_of,
            )
        )

    blocks: list[OutputBlock] = []
    if inputs.yield_curve is not None:
        blocks.append(_build_yield_curve_block(inputs.yield_curve, freshness_ts=inputs.as_of))
    else:
        blocks.append(
            _build_unavailable_block(
                block_id="q6.yield_curve_regime",
                bootstrap_reason="yield_curve: required FRED DGS series unavailable",
                freshness_ts=inputs.as_of,
            )
        )
    if inputs.inflation is not None:
        blocks.append(_build_inflation_block(inputs.inflation, freshness_ts=inputs.as_of))
    else:
        blocks.append(
            _build_unavailable_block(
                block_id="q6.inflation_regime",
                bootstrap_reason="inflation: T10YIE history unavailable",
                freshness_ts=inputs.as_of,
            )
        )
    if inputs.dollar_attribution is not None:
        blocks.append(
            _build_dollar_attribution_block(inputs.dollar_attribution, freshness_ts=inputs.as_of)
        )
    else:
        blocks.append(
            _build_unavailable_block(
                block_id="q6.dollar_attribution",
                bootstrap_reason="dollar_attribution: DTWEXBGS history unavailable",
                freshness_ts=inputs.as_of,
            )
        )
    blocks.append(_build_funding_stress_block(inputs.funding_stress, freshness_ts=inputs.as_of))
    blocks.append(_build_market_liquidity_block(inputs.market_liquidity, freshness_ts=inputs.as_of))
    for indicator, flag in inputs.macro_surprise_anomalies:
        blocks.append(
            _build_macro_surprise_anomaly_block(indicator, flag, freshness_ts=inputs.as_of)
        )
    return blocks


def compute_q6_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
) -> list[OutputBlock]:
    """Session-accepting shim that pre-loads :class:`Q6Inputs` and delegates.

    Existing call sites pass a ``Session`` directly; the shim invokes
    :func:`load_q6_inputs` (which performs the funding-stress and
    market-liquidity composite-refresh writes synchronously under the
    shared session) and then runs the pure compute. The orchestrator calls
    the pure compute directly under its TaskGroup; this shim is preserved
    for the external test suite at
    ``tests/distillation/external/test_q6_compute_blocks.py`` and any
    consumer not yet routed through the orchestrator.
    """
    inputs = load_q6_inputs(session, config=config, as_of=as_of)
    return assemble_q6_blocks_from_inputs(inputs)


__all__ = [
    "Q6Inputs",
    "assemble_q6_blocks",
    "assemble_q6_blocks_from_inputs",
    "compute_q6_blocks",
    "load_q6_inputs",
]
