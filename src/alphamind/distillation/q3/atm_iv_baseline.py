"""Q3 ATM-IV baseline state — quant 3a, ``atm_iv`` baseline kind.

Implements:
- :data:`ATM_IV_BASELINE_KIND` — baseline kind tag constant.
- :func:`refresh_atm_iv_baselines` — refresh per-ticker ATM-IV trailing
  baselines and compute IV-rank.
- Private helpers: :func:`_select_atm_iv_history`, :func:`_percentile_rank`,
  :func:`_upsert_atm_iv_baseline`.

Per ``threshold-calibration.md`` and the story-08b Notes section: the
existing ``distillation_ticker_baseline`` schema fits the IV-rank usage
natively (mean/stdev of trailing IV, with the percentile derived on read).
Extending the table avoids a new state-table family.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibratedValue, CalibrationState
from alphamind.persistence.models import (
    DistillationTickerBaseline,
    OptionsContracts,
    OptionsContractSnapshots,
)

ATM_IV_BASELINE_KIND = "atm_iv"


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


def _percentile_rank(values: Sequence[float], target: float) -> float:
    """Percentile of ``target`` against ``values`` using the ``<= target`` convention.

    Mirrors :func:`alphamind.distillation.baselines._percentile_rank` so the
    IV-rank read is consistent with the composite-state IV-rank read.
    """
    if not values:
        return 0.0
    le = sum(1 for x in values if x <= target)
    return float(le) / float(len(values)) * 100.0


def _upsert_atm_iv_baseline(
    session: Session,
    *,
    ticker: str,
    as_of: str,
    mean: float,
    stdev: float,
    n_observations: int,
    window_days: int,
    state: CalibrationState,
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
                mean=mean,
                stdev=stdev,
                n_observations=n_observations,
                window_days=window_days,
                calibration_state=state.value,
                ingested_at=as_of,
            )
        )
    else:
        existing.mean = mean
        existing.stdev = stdev
        existing.n_observations = n_observations
        existing.window_days = window_days
        existing.calibration_state = state.value
        existing.ingested_at = as_of


def refresh_atm_iv_baselines(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    window_days: int,
    min_observations: int,
) -> dict[str, CalibratedValue]:
    """Refresh per-ticker ATM-IV trailing baselines and compute the IV-rank.

    For each ticker in ``ticker_scope``:

    1. Pull the trailing ``window_days`` of ATM-call IV observations from
       ``options_contract_snapshots`` joined to ``options_contracts``.
    2. Compute the mean / stdev of the window.
    3. Compute the IV-rank percentile of the latest observation against
       the window.
    4. UPSERT a ``kind='atm_iv'`` row into ``distillation_ticker_baseline``.
    5. Return a :class:`CalibratedValue` carrying the mean / stdev /
       ``iv_rank_percentile``.

    Tickers with no IV observations in the window receive an UNAVAILABLE
    tag — the rank is undefined.
    """
    # Inline window-start computation: we don't need the time-arithmetic
    # helpers from baselines.py because the snapshot timestamps are already
    # ISO 8601 strings and SQLite's lexicographic ordering covers the range
    # filter directly. Match the convention by subtracting window_days.
    end_dt = datetime.fromisoformat(as_of).astimezone(UTC)
    start_dt = end_dt - timedelta(days=window_days)
    range_start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    out: dict[str, CalibratedValue] = {}
    for ticker in ticker_scope:
        history = _select_atm_iv_history(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=as_of,
        )
        n = len(history)
        if n == 0:
            out[ticker] = CalibratedValue(
                value=None,
                state=CalibrationState.UNAVAILABLE,
                bootstrap_reason=(f"atm_iv: 0 < {min_observations} (no observations in window)"),
            )
            continue
        mean = statistics.fmean(history)
        stdev = statistics.pstdev(history) if n > 1 else 0.0
        latest = history[-1]
        percentile = _percentile_rank(history, latest)
        state = CalibrationState.CALIBRATED if n >= min_observations else CalibrationState.BOOTSTRAP
        reason = (
            None
            if state is CalibrationState.CALIBRATED
            else f"atm_iv_min_observations: {n} < {min_observations}"
        )
        _upsert_atm_iv_baseline(
            session,
            ticker=ticker,
            as_of=as_of,
            mean=mean,
            stdev=stdev,
            n_observations=n,
            window_days=window_days,
            state=state,
        )
        out[ticker] = CalibratedValue(
            value={
                "mean": mean,
                "stdev": stdev,
                "n_observations": n,
                "window_days": window_days,
                "latest_iv": latest,
                "iv_rank_percentile": percentile,
            },
            state=state,
            bootstrap_reason=reason,
        )
    session.flush()
    return out
