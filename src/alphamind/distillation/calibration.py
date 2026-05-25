"""Calibration state and fallback framework — story 02-distillation/04, ALP-540.

Every per-category distillation computation routes its output through this
module so a uniform three-state ``calibration_state`` tag accompanies the
value. The tag flows downstream to domain researchers, the synthesizer, and
the strategist as ``Signal quality`` per
``docs/design/02-distillation-layer/threshold-calibration.md`` § Bootstrap
policy.

Three states exist (post-ALP-540 vocabulary):

- ``CALIBRATED`` — every input reached its ``*_min_observations`` threshold
  and the value comes from per-ticker / per-pair / per-contract rolling
  state.
- ``ACCUMULATING`` — at least one input has some observations but is below
  its minimum threshold; a cross-sectional pooled fallback substitutes for
  the missing per-key baseline. Time alone resolves the state — no operator
  action required. ``bootstrap_reason`` names the missing input.
- ``UNAVAILABLE`` — zero observations for the input (collector failure,
  vendor outage, missing series) or the cross-sectional pool itself is
  empty. The output is omitted and the operator must investigate; downstream
  consumers see a missing block rather than a misleading zero.

The framework is upstream of the persistence schema. The string values of
:class:`CalibrationState` equal the ``calibration_state`` CHECK-constraint
strings on the distillation tables; both this module and
``persistence.models`` import the vocabulary from
:mod:`alphamind._kernel.calibration` so the schema and the framework
cannot drift.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind._kernel.calibration import CALIBRATION_STATE_VALUES, CalibrationState
from alphamind.distillation._calibration_core import (
    CalibratedValue,
    combine_calibration_states,
    decide_calibration_state,
    tag_with_fallback,
)
from alphamind.persistence.models import (
    DistillationEventHistory,
    DistillationTickerBaseline,
    SectorClassification,
)

if TYPE_CHECKING:
    from alphamind.distillation._config_domain import (
        LeadLagDomainConfig,
        PredictionMarketDomainConfig,
    )

__all__ = [
    "CALIBRATION_STATE_VALUES",
    "EXTENDED_HOURS_BOOTSTRAP_RATE",
    "CalibratedValue",
    "CalibrationState",
    "combine_calibration_states",
    "decide_calibration_state",
    "default_lead_lag_pair_estimate",
    "pair_max_lag_days",
    "prediction_market_delta_default",
    "sector_pooled_atr_baseline",
    "sector_pooled_gap_fill_rate",
    "sector_pooled_volume_baseline",
    "tag_with_fallback",
    "universe_pooled_extended_hours_confirmation_rate",
    "universe_pooled_sentiment_distribution",
]

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

EXTENDED_HOURS_BOOTSTRAP_RATE: float = 0.5
"""Cold-start prior for the extended-hours confirmation rate.

``threshold-calibration.md`` § Bootstrap policy specifies "Cold-start
default 50% (no information)" for the universe-pooled extended-hours
confirmation rate. Encoded as a named constant rather than a literal so
the prior is discoverable and self-documenting.
"""


# ---------------------------------------------------------------------------
# Per-output value + tag wrapper + boundary helper
# ---------------------------------------------------------------------------
#
# :class:`CalibratedValue`, :func:`decide_calibration_state` and
# :func:`tag_with_fallback` live in :mod:`._calibration_core` (pure module,
# no sqlalchemy edges). They are re-exported here for backward
# compatibility — every existing import site continues to work.


# ---------------------------------------------------------------------------
# Cross-sectional fallback functions
# ---------------------------------------------------------------------------
#
# Each fallback returns the universe-wide or sector-pooled prior the layer
# substitutes for the missing per-key baseline. Per the framework contract,
# functions that aggregate over a possibly-empty pool return ``None``
# explicitly when the pool is empty — :func:`tag_with_fallback` then folds
# that into a :attr:`CalibrationState.UNAVAILABLE` block. Functions whose
# prior is always available (lead-lag default bound, prediction-market
# delta) return the prior unconditionally.


def _pool_calibrated_baselines(
    session: Session,
    *,
    baseline_kind: str,
    as_of: str,
    sector: str | None,
) -> tuple[float, float] | None:
    """Aggregate calibrated per-ticker baselines into a pooled ``(mean, stdev)``.

    Bootstrap-tagged baselines are excluded — they are themselves downstream
    of a fallback and would inject circular noise into the pool.

    When ``sector`` is ``None`` the pool spans the universe; otherwise it is
    restricted to tickers whose ``alphamind_sector`` matches. Returns
    ``None`` when the pool is empty.
    """
    stmt = select(DistillationTickerBaseline.mean, DistillationTickerBaseline.stdev).where(
        DistillationTickerBaseline.baseline_kind == baseline_kind,
        DistillationTickerBaseline.as_of == as_of,
        DistillationTickerBaseline.calibration_state == CalibrationState.CALIBRATED.value,
    )
    if sector is not None:
        stmt = stmt.join(
            SectorClassification,
            SectorClassification.ticker == DistillationTickerBaseline.ticker,
        ).where(SectorClassification.alphamind_sector == sector)
    rows = session.execute(stmt).all()
    if not rows:
        return None
    means = [float(row.mean) for row in rows]
    stdevs = [float(row.stdev) for row in rows]
    return (statistics.fmean(means), statistics.fmean(stdevs))


def sector_pooled_volume_baseline(
    session: Session,
    *,
    sector: str,
    as_of: str,
) -> tuple[float, float] | None:
    """Sector-pooled mean/stdev of calibrated per-ticker volume baselines.

    Used as the bootstrap fallback when a ticker's own volume baseline is
    below ``volume_baseline_days`` of observations. Returns ``None`` when
    the sector pool is itself empty.
    """
    return _pool_calibrated_baselines(session, baseline_kind="volume", sector=sector, as_of=as_of)


def sector_pooled_atr_baseline(
    session: Session,
    *,
    sector: str,
    as_of: str,
) -> tuple[float, float] | None:
    """Sector-pooled mean/stdev of calibrated per-ticker ATR baselines.

    Used as the bootstrap fallback when a ticker's own ATR baseline is below
    ``atr_baseline_days`` of observations. Returns ``None`` when the sector
    pool is itself empty.
    """
    return _pool_calibrated_baselines(session, baseline_kind="atr", sector=sector, as_of=as_of)


def universe_pooled_sentiment_distribution(
    session: Session,
    *,
    as_of: str,
) -> tuple[float, float] | None:
    """Universe-wide ``(mean, stdev)`` of calibrated per-ticker sentiment baselines.

    Sentiment is universe-pooled rather than sector-pooled per
    ``threshold-calibration.md`` § Bootstrap policy: the pooled prior loses
    the per-ticker range nuance (TSLA's range is wider than JPM's), which is
    precisely why the bootstrap tag matters downstream — domain researchers
    weight the percentile read accordingly.

    Returns ``None`` when no calibrated sentiment baseline exists for any
    ticker at ``as_of``.
    """
    return _pool_calibrated_baselines(session, baseline_kind="sentiment", sector=None, as_of=as_of)


def _event_outcome_rate(
    session: Session,
    *,
    event_kind: str,
    success_outcome: str,
    as_of: str,
    sector: str | None,
) -> float | None:
    """Aggregate event-history rows into ``count(success) / count(*)``.

    Counts only events with ``event_ts <= as_of`` so the rate is evaluable
    at any historical point. When ``sector`` is ``None`` the pool spans the
    universe; otherwise it is restricted to tickers in that sector. Returns
    ``None`` when no events of ``event_kind`` exist in scope.
    """
    total_stmt = select(func.count()).where(
        DistillationEventHistory.event_kind == event_kind,
        DistillationEventHistory.event_ts <= as_of,
    )
    if sector is not None:
        total_stmt = (
            total_stmt.select_from(DistillationEventHistory)
            .join(
                SectorClassification,
                SectorClassification.ticker == DistillationEventHistory.ticker,
            )
            .where(SectorClassification.alphamind_sector == sector)
        )
    success_stmt = total_stmt.where(
        DistillationEventHistory.outcome == success_outcome,
    )
    total = session.execute(total_stmt).scalar_one()
    if total == 0:
        return None
    successes = session.execute(success_stmt).scalar_one()
    return float(successes) / float(total)


def sector_pooled_gap_fill_rate(
    session: Session,
    *,
    sector: str,
    as_of: str,
) -> float | None:
    """Sector-pooled gap-fill rate over events at or before ``as_of``.

    The rate is ``count(outcome == 'filled') / count(*)`` over every gap
    event in the sector. Refreshed monthly per the spec, but the function
    itself is stateless — the caller decides whether to recompute or read a
    cached aggregate.

    Returns ``None`` when no gap events exist in the sector at ``as_of``.
    """
    return _event_outcome_rate(
        session,
        event_kind="gap",
        success_outcome="filled",
        sector=sector,
        as_of=as_of,
    )


def universe_pooled_extended_hours_confirmation_rate(
    session: Session,
    *,
    as_of: str,
) -> float:
    """Universe-pooled extended-hours confirmation rate over events at or before ``as_of``.

    The rate is ``count(outcome == 'confirmed') / count(*)`` over every
    extended-hours event in the universe. Refreshed monthly per the spec,
    but the function itself is stateless.

    On cold start — when no extended-hours events exist anywhere in the
    universe at ``as_of`` — returns :data:`EXTENDED_HOURS_BOOTSTRAP_RATE`
    rather than ``None``. Per the spec this prior is the deliberate "no
    information" baseline; downstream consumers see a tagged rate, not an
    UNAVAILABLE block.
    """
    rate = _event_outcome_rate(
        session,
        event_kind="extended_hours",
        success_outcome="confirmed",
        sector=None,
        as_of=as_of,
    )
    if rate is None:
        return EXTENDED_HOURS_BOOTSTRAP_RATE
    return rate


# ---------------------------------------------------------------------------
# Cross-sectional fallbacks that read from configuration, not from state
# ---------------------------------------------------------------------------


_LEAD_LAG_MAX_DAYS_FIELDS: dict[str, str] = {
    "funding_to_credit": "lead_lag_funding_to_credit_max_days",
    "credit_to_equity": "lead_lag_credit_to_equity_max_days",
    "semis_to_tech": "lead_lag_semis_to_tech_max_days",
    "financials_to_market": "lead_lag_financials_to_market_max_days",
    "commodity_to_energy_equity": "lead_lag_commodity_to_energy_equity_max_days",
}


def pair_max_lag_days(
    *,
    pair_key: str,
    lead_lag_config: LeadLagDomainConfig,
) -> int:
    """Return the configured ``_max_days`` ceiling for ``pair_key``.

    ``pair_key`` is the unprefixed pair identifier (e.g.
    ``"funding_to_credit"``). Raises ``KeyError`` for any unrecognized pair
    — the caller passed a name the configuration does not enumerate, which
    indicates a bug at the call site.
    """
    field_name = _LEAD_LAG_MAX_DAYS_FIELDS[pair_key]
    return int(getattr(lead_lag_config, field_name))


def default_lead_lag_pair_estimate(
    *,
    pair_key: str,
    lead_lag_config: LeadLagDomainConfig,
) -> int:
    """Return the Class A ``_max_days`` bound for ``pair_key`` as the prior.

    Per ``threshold-calibration.md`` § Bootstrap policy, lead-lag pair
    estimates begin updating after the first 10 observed pair events; until
    then the prior *is* the configured ``_max_days`` ceiling for the pair.
    """
    return pair_max_lag_days(pair_key=pair_key, lead_lag_config=lead_lag_config)


def prediction_market_delta_default(
    *,
    prediction_market_config: PredictionMarketDomainConfig,
) -> float:
    """Return the universe-wide prediction-market delta threshold.

    Per ``threshold-calibration.md`` § Bootstrap policy, the prediction-
    market delta has no per-contract bootstrap — every contract reads the
    same Class A ``prediction_market_delta_pp_threshold``. The "default"
    here is the prior the layer applies on day one and continues to apply
    indefinitely; the function exists for symmetry with the other
    fallbacks so each Class B output routes through a named prior.
    """
    return float(prediction_market_config.prediction_market_delta_pp_threshold)
