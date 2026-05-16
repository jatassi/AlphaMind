"""IO shell for the q3 ATM-IV trailing baseline (ALP-484).

Owns every DB read and write the IV-rank pipeline needs. The pure compute
lives in :mod:`.atm_iv_baseline_compute`. Three responsibilities live here
that the pure compute cannot carry:

1. :func:`load_atm_iv_history_by_ticker` — pre-loads the per-ticker
   trailing-window IV observations the compute consumes.
2. :func:`upsert_atm_iv_baselines` — applies the compute output's
   per-ticker :class:`AtmIvBaselineUpsertPayload` back to
   ``distillation_ticker_baseline``.
3. :func:`load_and_refresh_atm_iv_baselines` — composes the two halves
   plus the pure compute so q3's :class:`Q3Inputs` loader can pre-resolve
   ``iv_rank_results`` (a ``Mapping[str, CalibratedValue]``) before the
   Phase 2 ``TaskGroup`` dispatches the parallel pure compute.

The write path is kept on a raw ``Session`` rather than a Protocol method
because the pilot-scoped :class:`DistillationRepository` Protocol is
read-only (audit pre-resolved decision E).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation._calibration_core import CalibratedValue
from alphamind.distillation.q3.atm_iv_baseline_compute import (
    ATM_IV_BASELINE_KIND,
    AtmIvBaselineResult,
    AtmIvBaselineUpsertPayload,
    compute_atm_iv_baselines,
)
from alphamind.persistence.models import (
    DistillationTickerBaseline,
    OptionsContracts,
    OptionsContractSnapshots,
)


def _select_atm_iv_history(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> list[float]:
    """Return ATM-call IV observations for ``ticker`` ascending in time.

    The "ATM" contract is selected per snapshot as the call whose strike is
    closest to the snapshot's ``underlying_price``. Snapshots without an
    ``implied_volatility`` are skipped — they carry no observation.
    """
    stmt = (
        select(
            OptionsContractSnapshots.snapshot_ts,
            OptionsContractSnapshots.implied_volatility,
            OptionsContractSnapshots.underlying_price,
            OptionsContracts.strike_price,
        )
        .join(
            OptionsContracts,
            OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
        )
        .where(
            OptionsContractSnapshots.underlying_ticker == ticker,
            OptionsContracts.contract_type == "call",
            OptionsContractSnapshots.snapshot_ts >= range_start,
            OptionsContractSnapshots.snapshot_ts <= range_end,
            OptionsContractSnapshots.implied_volatility.isnot(None),
        )
        .order_by(OptionsContractSnapshots.snapshot_ts)
    )
    rows = session.execute(stmt).all()
    # Group by snapshot_ts; pick the strike closest to underlying_price per
    # snapshot. The grouping is small (one snapshot per day per ticker) so
    # a Python pass is cheaper than a self-join.
    by_ts: dict[str, tuple[float, float]] = {}
    for snapshot_ts, iv, underlying_price, strike in rows:
        if iv is None or underlying_price is None:
            continue
        gap = abs(float(strike) - float(underlying_price))
        existing = by_ts.get(snapshot_ts)
        if existing is None or gap < existing[1]:
            by_ts[snapshot_ts] = (float(iv), gap)
    return [by_ts[ts][0] for ts in sorted(by_ts)]


def load_atm_iv_history_by_ticker(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    window_days: int,
) -> dict[str, list[float]]:
    """Pre-load the per-ticker trailing-window IV history the compute consumes."""
    end_dt = datetime.fromisoformat(as_of).astimezone(UTC)
    start_dt = end_dt - timedelta(days=window_days)
    range_start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        ticker: _select_atm_iv_history(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=as_of,
        )
        for ticker in ticker_scope
    }


def _upsert_atm_iv_baseline(
    session: Session,
    *,
    ticker: str,
    as_of: str,
    payload: AtmIvBaselineUpsertPayload,
) -> None:
    existing = session.execute(
        select(DistillationTickerBaseline).where(
            DistillationTickerBaseline.ticker == ticker,
            DistillationTickerBaseline.baseline_kind == ATM_IV_BASELINE_KIND,
            DistillationTickerBaseline.as_of == as_of,
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            DistillationTickerBaseline(
                ticker=ticker,
                baseline_kind=ATM_IV_BASELINE_KIND,
                as_of=as_of,
                mean=payload.mean,
                stdev=payload.stdev,
                n_observations=payload.n_observations,
                window_days=payload.window_days,
                calibration_state=payload.state.value,
                ingested_at=as_of,
            )
        )
    else:
        existing.mean = payload.mean
        existing.stdev = payload.stdev
        existing.n_observations = payload.n_observations
        existing.window_days = payload.window_days
        existing.calibration_state = payload.state.value
        existing.ingested_at = as_of


def upsert_atm_iv_baselines(
    session: Session,
    *,
    results: Mapping[str, AtmIvBaselineResult],
    as_of: str,
) -> None:
    """Apply each non-``UNAVAILABLE`` result's upsert payload to the baseline table."""
    for ticker, result in results.items():
        if result.upsert is None:
            continue
        _upsert_atm_iv_baseline(session, ticker=ticker, as_of=as_of, payload=result.upsert)
    session.flush()


def load_and_refresh_atm_iv_baselines(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    window_days: int,
    min_observations: int,
) -> dict[str, CalibratedValue]:
    """Pre-load history → pure compute → upsert; return the IV-rank map.

    This is the loader-side composition the q3-wide loader (and the legacy
    session-accepting shim) call. The session work brackets the pure
    compute so the resulting :class:`Q3Inputs` carries ``iv_rank_results``
    as already-calibrated values — the Phase 2 pure compute consumes them
    without further DB access.
    """
    history_by_ticker = load_atm_iv_history_by_ticker(
        session,
        ticker_scope=ticker_scope,
        as_of=as_of,
        window_days=window_days,
    )
    results = compute_atm_iv_baselines(
        history_by_ticker,
        window_days=window_days,
        min_observations=min_observations,
    )
    upsert_atm_iv_baselines(session, results=results, as_of=as_of)
    return {ticker: result.rank for ticker, result in results.items()}


__all__ = [
    "load_and_refresh_atm_iv_baselines",
    "load_atm_iv_history_by_ticker",
    "upsert_atm_iv_baselines",
]
