"""Pure-compute core for q3 options anomaly detections (ALP-484).

Splits the session-bound :mod:`.anomalies` module into pure compute over
frozen inputs (this module) and an IO shell that owns the DB reads
(:mod:`.anomalies_loaders`). The pure compute imports zero ORM types so
the orchestrator can run :func:`compute_sector_wide_sweeps` inside an
``asyncio.to_thread`` call without touching the shared SQLAlchemy session.

Two detections live here, both pure:

* :func:`compute_low_oi_volume_anomalies` — quant 3b. Operates over a
  pre-loaded list of (today snapshot + prior volume history) pairs.
* :func:`compute_sector_wide_sweeps` — quant 3g. Operates over a pre-loaded
  ``ticker → sector`` membership map plus per-ticker BTO flow z-scores.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

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


@dataclass(frozen=True, slots=True)
class LowOiSnapshotPair:
    """Per-contract snapshot at ``as_of`` plus its prior-volume history.

    The loader pre-builds one entry per contract whose ``as_of`` snapshot
    carries non-null ``volume_today`` + ``open_interest``; ``prior_volumes``
    is the chronologically-prior set of non-null ``volume_today`` readings
    used for the trailing-average gate. Order does not affect the gate.
    """

    contract_ticker: str
    underlying_ticker: str
    snapshot_ts: str
    volume_today: int
    open_interest: int
    prior_volumes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LowOiVolumeAnomalyInputs:
    """Frozen inputs for :func:`compute_low_oi_volume_anomalies`."""

    snapshots: tuple[LowOiSnapshotPair, ...]


def compute_low_oi_volume_anomalies(
    inputs: LowOiVolumeAnomalyInputs,
    *,
    volume_multiple_threshold: float,
    oi_threshold: int,
) -> list[LowOiVolumeAnomaly]:
    """Flag the per-contract low-OI volume anomaly over pre-loaded inputs.

    Mirrors the legacy session-bound :func:`detect_low_oi_volume_anomalies`
    behavior: a snapshot fires when ``open_interest < oi_threshold`` AND
    ``volume_today / trailing_avg_volume >= volume_multiple_threshold``.
    Snapshots with no prior history are skipped — the multiple is undefined
    against an empty history.
    """
    anomalies: list[LowOiVolumeAnomaly] = []
    for snapshot in inputs.snapshots:
        if snapshot.open_interest >= oi_threshold:
            continue
        if not snapshot.prior_volumes:
            continue
        trailing_avg = sum(snapshot.prior_volumes) / len(snapshot.prior_volumes)
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
                volume_today=snapshot.volume_today,
                trailing_avg_volume=trailing_avg,
                volume_multiple=multiple,
                open_interest=snapshot.open_interest,
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


@dataclass(frozen=True, slots=True)
class SectorSweepInputs:
    """Frozen inputs for :func:`compute_sector_wide_sweeps`.

    ``sector_membership`` maps each ticker to its ``alphamind_sector``;
    tickers absent from this map are silently skipped (no audience to route
    the finding). ``flow_zscores`` is the per-ticker call/put BTO z-score
    map from the q3 flow-z-score loader.
    """

    sector_membership: Mapping[str, str]
    flow_zscores: Mapping[str, FlowZScore]


def compute_sector_wide_sweeps(
    inputs: SectorSweepInputs,
    *,
    sigma_threshold: float,
    min_names: int,
) -> list[SectorWideSweep]:
    """Group flow z-scores by sector and emit sweeps with at least ``min_names``.

    For each sector and each direction (calls / puts), enumerate tickers
    whose BTO flow z-score on that direction is at least ``sigma_threshold``.
    When the count is at least ``min_names``, emit one
    :class:`SectorWideSweep` per ``(sector, direction)``.

    Tickers absent from ``sector_membership`` are skipped — distillation
    cannot route a finding without an audience.
    """
    by_sector_call: dict[str, list[str]] = {}
    by_sector_put: dict[str, list[str]] = {}
    for ticker, score in inputs.flow_zscores.items():
        sector = inputs.sector_membership.get(ticker)
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


__all__ = [
    "LowOiSnapshotPair",
    "LowOiVolumeAnomaly",
    "LowOiVolumeAnomalyInputs",
    "SectorSweepInputs",
    "SectorWideSweep",
    "SweepDirection",
    "compute_low_oi_volume_anomalies",
    "compute_sector_wide_sweeps",
]
