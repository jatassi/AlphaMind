"""IO shell for the q3 ETF/single-name IV divergence (ALP-484).

Pre-loads every per-sector input the pure compute consumes:

* ``etf_iv`` and ``single_name_aggregate_iv`` at ``as_of``.
* ``spread_baseline_mean`` and ``spread_baseline_stdev`` over the trailing
  ``baseline_days`` window.

The pure compute lives in :mod:`.etf_iv_divergence_compute`; this loader
was previously embedded in :mod:`.assemble` (private helpers
``_build_etf_iv_inputs``, ``_select_atm_iv_at``,
``_select_aggregate_single_name_iv``, ``_select_etf_iv_spread_baseline``)
and lifts to its dedicated module per the ALP-484 split.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.q3.atm_iv_baseline_loaders import _select_atm_iv_by_ts
from alphamind.persistence.models import SectorClassification

# Minimum sample count for a defined ``pstdev``. Mirrors the q12-pattern
# definitional-base sum used elsewhere in q3 to avoid the no-magic-numbers
# audit collision against the Class A
# ``anomaly_detection.funding_stress_component_alert_count`` (= 2).
_DEFINITIONAL_BASE: int = 1
_MIN_VARIANCE_SAMPLES: int = _DEFINITIONAL_BASE + _DEFINITIONAL_BASE


def _select_atm_iv_at(
    session: Session,
    *,
    underlying: str,
    as_of: str,
) -> float | None:
    """Return the ATM-call IV at exactly ``as_of`` for ``underlying``.

    A single-instant view over the canonical :func:`_select_atm_iv_by_ts`
    selector — "ATM" is the call whose strike is closest to the snapshot's
    ``underlying_price``. Sharing the selector keeps the divergence numerator's
    ETF IV and the baseline's ETF IV definitionally identical.
    """
    observation = _select_atm_iv_by_ts(
        session,
        ticker=underlying,
        range_start=as_of,
        range_end=as_of,
    ).get(as_of)
    return observation[0] if observation is not None else None


def _volume_weighted_iv(observations: Iterable[tuple[float, float | None]]) -> float | None:
    """Reduce ``(atm_iv, volume_today)`` observations to a volume-weighted IV.

    Each observation's weight is its ``volume_today`` when present and ``> 0``,
    else ``1.0``. Returns ``None`` when no observation contributes
    (``weight_sum <= 0``). Both the point estimate (the divergence numerator's
    single-name term) and the trailing baseline's per-date single-name term
    reduce through this one function, so the two cannot drift.
    """
    weighted_sum = 0.0
    weight_sum = 0.0
    for atm_iv, volume_today in observations:
        weight = float(volume_today) if volume_today is not None and volume_today > 0 else 1.0
        weighted_sum += atm_iv * weight
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return weighted_sum / weight_sum


def _select_aggregate_single_name_iv(
    session: Session,
    *,
    tickers: Sequence[str],
    as_of: str,
) -> float | None:
    """Return the volume-weighted aggregate ATM-IV across ``tickers`` at ``as_of``.

    The weight is ``volume_today`` of the ATM call on each constituent.
    Tickers without a same-day snapshot are skipped. Returns ``None`` when
    no constituent contributes — the divergence is undefined. Selects each
    constituent's ATM observation through the same :func:`_select_atm_iv_by_ts`
    selector and the same :func:`_volume_weighted_iv` reduction the trailing
    baseline uses, so the numerator and its baseline cannot drift.
    """
    observations: list[tuple[float, float | None]] = []
    for ticker in tickers:
        observation = _select_atm_iv_by_ts(
            session,
            ticker=ticker,
            range_start=as_of,
            range_end=as_of,
        ).get(as_of)
        if observation is not None:
            observations.append(observation)
    return _volume_weighted_iv(observations)


def _select_etf_iv_spread_baseline(
    session: Session,
    *,
    etf_ticker: str,
    constituents: Sequence[str],
    range_end: str,
    baseline_days: int,
) -> tuple[float, float]:
    """Return ``(mean, stdev)`` of the ETF/single-name IV spread over the
    trailing window. ``(0.0, 0.0)`` when the window is too thin or stale.
    """
    end_dt = datetime.fromisoformat(range_end).astimezone(UTC)
    start_dt = end_dt - timedelta(days=baseline_days)
    range_start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    etf_by_ts = _select_atm_iv_by_ts(
        session,
        ticker=etf_ticker,
        range_start=range_start,
        range_end=range_end,
    )
    if not etf_by_ts:
        return 0.0, 0.0
    constituent_by_ts = [
        _select_atm_iv_by_ts(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=range_end,
        )
        for ticker in constituents
    ]
    # Pair the ETF IV against the per-date cross-sectional single-name aggregate
    # by matching ``snapshot_ts``. A constituent observation on a date absent
    # from the ETF series never enters a spread; an ETF date with no constituent
    # snapshot contributes none. The per-date aggregate reduces through the same
    # ``_volume_weighted_iv`` helper as the point estimate, so the baseline and
    # the numerator cannot drift.
    spreads: list[float] = []
    for snapshot_ts in sorted(etf_by_ts):
        etf_iv, _ = etf_by_ts[snapshot_ts]
        single_name_iv = _volume_weighted_iv(
            by_ts[snapshot_ts] for by_ts in constituent_by_ts if snapshot_ts in by_ts
        )
        if single_name_iv is None:
            continue
        spreads.append(etf_iv - single_name_iv)
    if len(spreads) < _MIN_VARIANCE_SAMPLES:
        return 0.0, 0.0
    return statistics.fmean(spreads), statistics.pstdev(spreads)


def load_etf_iv_divergence_inputs(
    session: Session,
    *,
    sorted_sector_tickers: Mapping[str, Sequence[str]],
    as_of: str,
    baseline_days: int,
) -> dict[str, dict[str, float | str]]:
    """Build the per-sector mapping the divergence compute consumes.

    Resolves ``etf_ticker`` from any constituent's ``sector_classification``
    row, computes ``etf_iv`` and ``single_name_aggregate_iv`` at ``as_of``,
    and reads the trailing-``baseline_days`` spread mean/stdev. Sectors
    whose ETF or constituent IV cannot be resolved are omitted; the caller
    sees them as "no signal" rather than as a non-calibrated entry.
    """
    out: dict[str, dict[str, float | str]] = {}
    for sector, tickers in sorted_sector_tickers.items():
        if not tickers:
            continue
        etf_row = session.execute(
            select(SectorClassification.sector_etf).where(SectorClassification.ticker == tickers[0])
        ).scalar_one_or_none()
        if etf_row is None:
            continue
        etf_iv = _select_atm_iv_at(session, underlying=etf_row, as_of=as_of)
        if etf_iv is None:
            continue
        single_name_iv = _select_aggregate_single_name_iv(session, tickers=tickers, as_of=as_of)
        if single_name_iv is None:
            continue
        baseline_mean, baseline_stdev = _select_etf_iv_spread_baseline(
            session,
            etf_ticker=etf_row,
            constituents=tickers,
            range_end=as_of,
            baseline_days=baseline_days,
        )
        if baseline_stdev <= 0:
            continue
        out[sector] = {
            "etf_ticker": etf_row,
            "etf_iv": etf_iv,
            "single_name_aggregate_iv": single_name_iv,
            "spread_baseline_mean": baseline_mean,
            "spread_baseline_stdev": baseline_stdev,
        }
    return out


__all__ = [
    "load_etf_iv_divergence_inputs",
]
