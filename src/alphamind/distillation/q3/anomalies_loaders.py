"""IO shell for q3 options anomaly detections (ALP-484).

Owns every DB read the anomaly detections need. The pure compute lives in
:mod:`.anomalies_compute`. Two loader pairings live here:

* :func:`load_low_oi_volume_anomaly_inputs` — pulls the per-contract latest
  snapshot at ``as_of`` plus its prior-volume history. Uses raw ``Session``
  reads because the Protocol is read-only and pilot-scoped (audit
  pre-resolved decision E); adding a one-off Protocol method for this
  detection would expand the seam beyond the pilot intent.
* :func:`load_sector_sweep_inputs` — uses the
  :class:`alphamind.distillation._repository.DistillationRepository`
  Protocol's ``load_sector_classifications`` to project the per-ticker
  sector membership the sweep compute consumes.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation._repository import DistillationRepository
from alphamind.distillation.q3.anomalies_compute import (
    LowOiSnapshotPair,
    LowOiVolumeAnomalyInputs,
    SectorSweepInputs,
)
from alphamind.distillation.q3.pair_trade import FlowZScore
from alphamind.persistence.models import OptionsContractSnapshots


def load_low_oi_volume_anomaly_inputs(
    session: Session,
    *,
    as_of: str,
) -> LowOiVolumeAnomalyInputs:
    """Pre-load every contract's snapshot pair for the low-OI compute.

    Mirrors the legacy session-bound :func:`detect_low_oi_volume_anomalies`
    queries: pulls all snapshots whose ``snapshot_ts == as_of`` with non-null
    volume + OI; for each, pulls the prior volume readings strictly before
    ``as_of``. Snapshots with no prior history are still included so the
    compute side decides skip semantics in one place.
    """
    latest_stmt = (
        select(OptionsContractSnapshots)
        .where(OptionsContractSnapshots.snapshot_ts == as_of)
        .order_by(OptionsContractSnapshots.contract_ticker)
    )
    latest_rows = list(session.execute(latest_stmt).scalars().all())
    pairs: list[LowOiSnapshotPair] = []
    for snapshot in latest_rows:
        if snapshot.volume_today is None or snapshot.open_interest is None:
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
        prior_volumes = tuple(
            int(v) for v in session.execute(prior_stmt).scalars().all() if v is not None
        )
        pairs.append(
            LowOiSnapshotPair(
                contract_ticker=snapshot.contract_ticker,
                underlying_ticker=snapshot.underlying_ticker,
                snapshot_ts=snapshot.snapshot_ts,
                volume_today=int(snapshot.volume_today),
                open_interest=int(snapshot.open_interest),
                prior_volumes=prior_volumes,
            )
        )
    return LowOiVolumeAnomalyInputs(snapshots=tuple(pairs))


def load_sector_sweep_inputs(
    repository: DistillationRepository,
    *,
    flow_zscores: Mapping[str, FlowZScore],
) -> SectorSweepInputs:
    """Resolve per-ticker sector membership for the sweep compute.

    Only the tickers present in ``flow_zscores`` matter — the legacy
    full-table scan loaded every row but discarded ones missing from the
    z-score map.
    """
    if not flow_zscores:
        return SectorSweepInputs(sector_membership={}, flow_zscores=flow_zscores)
    sector_rows = repository.load_sector_classifications(tickers=tuple(flow_zscores))
    membership = {ticker: row.alphamind_sector for ticker, row in sector_rows.items()}
    return SectorSweepInputs(sector_membership=membership, flow_zscores=flow_zscores)


__all__ = [
    "load_low_oi_volume_anomaly_inputs",
    "load_sector_sweep_inputs",
]
