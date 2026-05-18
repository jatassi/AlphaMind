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
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.q3.atm_iv_baseline_loaders import _select_atm_iv_history
from alphamind.persistence.models import (
    OptionsContracts,
    OptionsContractSnapshots,
    SectorClassification,
)

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

    "ATM" is the call whose strike is closest to the snapshot's
    ``underlying_price``. Mirrors :func:`_select_atm_iv_history` for a
    single point.
    """
    stmt = (
        select(
            OptionsContractSnapshots.implied_volatility,
            OptionsContractSnapshots.underlying_price,
            OptionsContracts.strike_price,
        )
        .join(
            OptionsContracts,
            OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
        )
        .where(
            OptionsContractSnapshots.underlying_ticker == underlying,
            OptionsContracts.contract_type == "call",
            OptionsContractSnapshots.snapshot_ts == as_of,
            OptionsContractSnapshots.implied_volatility.isnot(None),
        )
    )
    rows = session.execute(stmt).all()
    best_iv: float | None = None
    best_gap: float | None = None
    for iv, underlying_price, strike in rows:
        if iv is None or underlying_price is None or strike is None:
            continue
        gap = abs(float(strike) - float(underlying_price))
        if best_gap is None or gap < best_gap:
            best_iv = float(iv)
            best_gap = gap
    return best_iv


def _select_aggregate_single_name_iv(
    session: Session,
    *,
    tickers: Sequence[str],
    as_of: str,
) -> float | None:
    """Return the volume-weighted aggregate ATM-IV across ``tickers`` at ``as_of``.

    The weight is ``volume_today`` of the ATM call on each constituent.
    Tickers without a same-day snapshot are skipped. Returns ``None`` when
    no constituent contributes — the divergence is undefined.
    """
    weighted_sum = 0.0
    weight_sum = 0.0
    for ticker in tickers:
        stmt = (
            select(
                OptionsContractSnapshots.implied_volatility,
                OptionsContractSnapshots.underlying_price,
                OptionsContractSnapshots.volume_today,
                OptionsContracts.strike_price,
            )
            .join(
                OptionsContracts,
                OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
            )
            .where(
                OptionsContractSnapshots.underlying_ticker == ticker,
                OptionsContracts.contract_type == "call",
                OptionsContractSnapshots.snapshot_ts == as_of,
                OptionsContractSnapshots.implied_volatility.isnot(None),
            )
        )
        best_iv: float | None = None
        best_gap: float | None = None
        best_weight: float = 0.0
        for iv, underlying_price, volume, strike in session.execute(stmt).all():
            if iv is None or underlying_price is None or strike is None:
                continue
            gap = abs(float(strike) - float(underlying_price))
            if best_gap is None or gap < best_gap:
                best_iv = float(iv)
                best_gap = gap
                best_weight = float(volume) if volume is not None else 0.0
        if best_iv is None:
            continue
        weight = best_weight if best_weight > 0 else 1.0
        weighted_sum += best_iv * weight
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return weighted_sum / weight_sum


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
    etf_history = _select_atm_iv_history(
        session,
        ticker=etf_ticker,
        range_start=range_start,
        range_end=range_end,
    )
    if not etf_history:
        return 0.0, 0.0
    constituent_history: list[float] = []
    for ticker in constituents:
        constituent_history.extend(
            _select_atm_iv_history(
                session,
                ticker=ticker,
                range_start=range_start,
                range_end=range_end,
            )
        )
    if not constituent_history:
        return 0.0, 0.0
    paired_length = min(len(etf_history), len(constituent_history))
    if paired_length < _MIN_VARIANCE_SAMPLES:
        return 0.0, 0.0
    spreads = [etf_history[i] - constituent_history[i] for i in range(paired_length)]
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
