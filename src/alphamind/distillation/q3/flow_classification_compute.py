"""Pure-compute core for q3 options flow classification (ALP-467).

Propagation proof of the compute/load split pattern piloted by q1/gap. The
classification logic that operates on snapshot pairs is pure; the
repository-driven shell lives in :mod:`.flow_classification_loaders`.

The flow-classification compute consumes a frozen
:class:`FlowClassificationInputs` carrying every contract + snapshot pair
the repository has pre-loaded for a ticker scope. The pure compute then
applies the OI-delta heuristic to bucket today's volume into BTO/STO
halves; no further DB access is required.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from alphamind.distillation._repository import (
    OptionsContractRow,
    OptionsContractSnapshotRow,
    TickerADVRow,
)

# Re-exported for downstream stability; the constant lives in the legacy
# module today but its canonical home is the compute core now.
SNAPSHOT_OI_DELTA_ATTRIBUTION = "snapshot_oi_delta"
"""Attribution tag carried on every classification output."""


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerContractSnapshotPair:
    """Today's snapshot + the most recent prior snapshot for one contract."""

    contract: OptionsContractRow
    today_snapshot: OptionsContractSnapshotRow | None
    prior_snapshot: OptionsContractSnapshotRow | None


@dataclass(frozen=True, slots=True)
class FlowClassificationInputs:
    """Frozen inputs for :func:`compute_options_flow`.

    ``per_ticker_pairs`` maps each underlying ticker to a tuple of
    :class:`PerContractSnapshotPair` covering every option contract on
    that ticker. The repository pre-loads this in one pass; the compute
    consumes it without further IO.
    """

    per_ticker_pairs: Mapping[str, tuple[PerContractSnapshotPair, ...]]


@dataclass(frozen=True, slots=True)
class PutFlowIntentInputs:
    """Frozen inputs for :func:`compute_put_flow_intent`.

    ``adv_by_ticker`` maps each ticker to its ADV row (or ``None`` when
    the ticker is unknown to ``asset_universe``).
    """

    system_long_positions: Mapping[str, int]
    adv_by_ticker: Mapping[str, TickerADVRow | None]


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickerOptionsFlow:
    """Per-ticker aggregated options flow classification."""

    ticker: str
    call_bto_volume: int
    call_sto_volume: int
    put_bto_volume: int
    put_sto_volume: int
    attribution_method: str


PutFlowIntent = Literal["protective", "speculative"]


# ---------------------------------------------------------------------------
# Pure compute
# ---------------------------------------------------------------------------


def compute_options_flow(
    inputs: FlowClassificationInputs,
) -> dict[str, TickerOptionsFlow]:
    """Aggregate today's options flow by underlying using the OI-delta heuristic.

    Pure compute over pre-loaded snapshot pairs; no DB access.
    """
    out: dict[str, TickerOptionsFlow] = {}
    for ticker, pairs in inputs.per_ticker_pairs.items():
        buckets: dict[tuple[str, str], int] = {
            ("call", "bto"): 0,
            ("call", "sto"): 0,
            ("put", "bto"): 0,
            ("put", "sto"): 0,
        }
        for pair in pairs:
            snapshot = pair.today_snapshot
            if snapshot is None or snapshot.volume_today is None:
                continue
            volume = int(snapshot.volume_today)
            if volume == 0:
                continue
            prior = pair.prior_snapshot
            prior_oi = (
                int(prior.open_interest)
                if prior is not None and prior.open_interest is not None
                else 0
            )
            current_oi = (
                int(snapshot.open_interest) if snapshot.open_interest is not None else prior_oi
            )
            side = "bto" if current_oi > prior_oi else "sto"
            buckets[(pair.contract.contract_type, side)] += volume
        out[ticker] = TickerOptionsFlow(
            ticker=ticker,
            call_bto_volume=buckets[("call", "bto")],
            call_sto_volume=buckets[("call", "sto")],
            put_bto_volume=buckets[("put", "bto")],
            put_sto_volume=buckets[("put", "sto")],
            attribution_method=SNAPSHOT_OI_DELTA_ATTRIBUTION,
        )
    return out


def compute_put_flow_intent(
    inputs: PutFlowIntentInputs,
    *,
    protective_holding_pct_of_adv: float,
) -> dict[str, PutFlowIntent]:
    """Tag each ticker's put flow as ``protective`` or ``speculative``.

    Pure compute over pre-loaded holdings + ADV; no DB access.
    """
    out: dict[str, PutFlowIntent] = {}
    for ticker, adv_row in inputs.adv_by_ticker.items():
        held_shares = int(inputs.system_long_positions.get(ticker, 0))
        if held_shares <= 0:
            out[ticker] = "speculative"
            continue
        if adv_row is None or adv_row.avg_daily_volume_shares is None:
            out[ticker] = "speculative"
            continue
        adv = float(adv_row.avg_daily_volume_shares)
        if adv <= 0:
            out[ticker] = "speculative"
            continue
        held_pct = (held_shares / adv) * 100.0
        out[ticker] = "protective" if held_pct >= protective_holding_pct_of_adv else "speculative"
    return out


__all__ = [
    "SNAPSHOT_OI_DELTA_ATTRIBUTION",
    "FlowClassificationInputs",
    "PerContractSnapshotPair",
    "PutFlowIntent",
    "PutFlowIntentInputs",
    "TickerOptionsFlow",
    "compute_options_flow",
    "compute_put_flow_intent",
]
