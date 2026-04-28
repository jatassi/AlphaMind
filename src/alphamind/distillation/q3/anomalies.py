"""Q3 options anomaly detections — low-OI volume anomaly and sector-wide sweeps.

Implements:
- :func:`detect_low_oi_volume_anomalies` — quant 3b: per-contract low-OI
  volume spike detection.
- :func:`detect_sector_wide_sweeps` — quant 3g: sector-level BTO sweep
  detection from per-ticker flow z-scores.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.persistence.models import (
    OptionsContractSnapshots,
    SectorClassification,
)

if TYPE_CHECKING:
    from alphamind.distillation.q3.pair_trade import FlowZScore


# ---------------------------------------------------------------------------
# Low-OI volume anomaly — quant 3b
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LowOiVolumeAnomaly:
    """One contract whose ``volume_today`` clears the 5x trailing-avg gate
    on a strike whose ``open_interest`` is below the OI floor.

    The data model intentionally exposes the underlying observation values
    (``volume_today``, ``trailing_avg_volume``, ``volume_multiple``,
    ``open_interest``) so a downstream consumer can render the anomaly
    payload without re-reading the snapshot table.
    """

    contract_ticker: str
    underlying_ticker: str
    snapshot_ts: str
    volume_today: int
    trailing_avg_volume: float
    volume_multiple: float
    open_interest: int


def detect_low_oi_volume_anomalies(
    session: Session,
    *,
    as_of: str,
    volume_multiple_threshold: float,
    oi_threshold: int,
) -> list[LowOiVolumeAnomaly]:
    """Scan options snapshots at ``as_of`` for the low-OI volume anomaly.

    For each contract whose latest snapshot at or before ``as_of`` carries a
    non-null ``volume_today`` and ``open_interest``: compute the trailing
    average volume from prior snapshots (strictly before ``as_of``); flag
    when ``volume_today / trailing_avg >= volume_multiple_threshold`` AND
    ``open_interest < oi_threshold``. Contracts with no prior history are
    skipped — the multiple is undefined against an empty history.

    The two threshold gates are independent: ``volume_multiple_threshold``
    governs the volume side, ``oi_threshold`` governs the OI side.
    """
    # Pull the latest snapshot per contract at ``as_of``.
    latest_stmt = (
        select(OptionsContractSnapshots)
        .where(OptionsContractSnapshots.snapshot_ts == as_of)
        .order_by(OptionsContractSnapshots.contract_ticker)
    )
    latest_rows = list(session.execute(latest_stmt).scalars().all())
    anomalies: list[LowOiVolumeAnomaly] = []
    for snapshot in latest_rows:
        if snapshot.volume_today is None or snapshot.open_interest is None:
            continue
        if snapshot.open_interest >= oi_threshold:
            continue
        prior_stmt = (
            select(OptionsContractSnapshots.volume_today)
            .where(
                OptionsContractSnapshots.contract_ticker == snapshot.contract_ticker,
                OptionsContractSnapshots.snapshot_ts < as_of,
                OptionsContractSnapshots.volume_today.isnot(None),
            )
            .order_by(OptionsContractSnapshots.snapshot_ts.desc())
        )
        prior_volumes = [
            int(v) for v in session.execute(prior_stmt).scalars().all() if v is not None
        ]
        if not prior_volumes:
            continue
        trailing_avg = sum(prior_volumes) / len(prior_volumes)
        if trailing_avg <= 0:
            continue
        multiple = snapshot.volume_today / trailing_avg
        if multiple < volume_multiple_threshold:
            continue
        anomalies.append(
            LowOiVolumeAnomaly(
                contract_ticker=snapshot.contract_ticker,
                underlying_ticker=snapshot.underlying_ticker,
                snapshot_ts=snapshot.snapshot_ts,
                volume_today=int(snapshot.volume_today),
                trailing_avg_volume=trailing_avg,
                volume_multiple=multiple,
                open_interest=int(snapshot.open_interest),
            )
        )
    return anomalies


# ---------------------------------------------------------------------------
# Sector-wide sweep detection — quant 3g
# ---------------------------------------------------------------------------


SweepDirection = Literal["call", "put"]


@dataclass(frozen=True, slots=True)
class SectorWideSweep:
    """One detected sector-wide sweep on calls or puts.

    The sweep names every sector ticker whose BTO flow on ``direction``
    cleared ``sigma_threshold``. The minimum-names gate ensures isolated
    spikes do not register.
    """

    sector: str
    direction: SweepDirection
    tickers: tuple[str, ...]


def _sector_membership(session: Session) -> dict[str, str]:
    """Return ``{ticker: alphamind_sector}`` for the universe."""
    stmt = select(SectorClassification.ticker, SectorClassification.alphamind_sector)
    return {row[0]: row[1] for row in session.execute(stmt).all()}


def detect_sector_wide_sweeps(
    session: Session,
    *,
    flow_zscores: Mapping[str, FlowZScore],
    sigma_threshold: float,
    min_names: int,
) -> list[SectorWideSweep]:
    """Group flow z-scores by sector and emit sweeps with at least ``min_names``.

    For each sector and each direction (calls / puts), enumerate tickers
    whose BTO flow z-score on that direction is at least ``sigma_threshold``.
    When the count is at least ``min_names``, emit one
    :class:`SectorWideSweep` per ``(sector, direction)``.

    Tickers absent from ``sector_classification`` are skipped — distillation
    cannot route a finding without an audience.
    """
    membership = _sector_membership(session)
    by_sector_call: dict[str, list[str]] = {}
    by_sector_put: dict[str, list[str]] = {}
    for ticker, score in flow_zscores.items():
        sector = membership.get(ticker)
        if sector is None:
            continue
        if score.call_bto_z >= sigma_threshold:
            by_sector_call.setdefault(sector, []).append(ticker)
        if score.put_bto_z >= sigma_threshold:
            by_sector_put.setdefault(sector, []).append(ticker)

    sweeps: list[SectorWideSweep] = []
    for sector in sorted(by_sector_call):
        names = by_sector_call[sector]
        if len(names) >= min_names:
            sweeps.append(
                SectorWideSweep(
                    sector=sector,
                    direction="call",
                    tickers=tuple(sorted(names)),
                )
            )
    for sector in sorted(by_sector_put):
        names = by_sector_put[sector]
        if len(names) >= min_names:
            sweeps.append(
                SectorWideSweep(
                    sector=sector,
                    direction="put",
                    tickers=tuple(sorted(names)),
                )
            )
    return sweeps
