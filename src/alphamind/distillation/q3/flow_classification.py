"""Q3 options flow classification — BTO/STO heuristic and related signals.

Implements:
- :data:`SNAPSHOT_OI_DELTA_ATTRIBUTION` — attribution tag constant.
- :class:`TickerOptionsFlow` — per-ticker aggregated flow result.
- :func:`classify_options_flow` — aggregate today's BTO/STO volumes per ticker.
- :class:`PutFlowIntent` — ``protective`` vs. ``speculative`` taxonomy.
- :func:`classify_put_flow_intent` — tag put-flow intent per ticker.
- :class:`IndexVsSectorLabel` — three-label index-vs-sector taxonomy.
- :class:`IndexVsSectorClassification` — one index-vs-sector finding.
- :func:`classify_index_vs_sector_flow` — classify index/sector put-flow relationship.
- Private snapshot-fetch helpers used by the classifiers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.persistence.models import (
    AssetUniverse,
    OptionsContracts,
    OptionsContractSnapshots,
)

if TYPE_CHECKING:
    from alphamind.distillation.q3.pair_trade import FlowZScore

# ---------------------------------------------------------------------------
# Module-level constants documenting the attribution choice.
# ---------------------------------------------------------------------------

SNAPSHOT_OI_DELTA_ATTRIBUTION = "snapshot_oi_delta"
"""Tag carried on every flow-classification output.

Per the story: Polygon's snapshot endpoint doesn't carry trade-side
attribution for individual prints. The OI-delta heuristic is the standard
workaround (an opening trade pair adds to OI, a closing pair reduces it).
The tag lets downstream readers know the attribution limit.
"""

# ---------------------------------------------------------------------------
# BTO/STO classification heuristic — quant 3b, 3f
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickerOptionsFlow:
    """Per-ticker aggregated options flow classification for the current window.

    The four counts split today's volume into the BTO/STO halves under the
    snapshot-OI-delta heuristic: an opening trade pair (BTO) increases the
    contract's OI day-over-day; a closing trade pair (STO/STC) flat-or-
    decreases it. Counts are summed across every contract on the underlying.

    ``attribution_method`` is always :data:`SNAPSHOT_OI_DELTA_ATTRIBUTION`
    so downstream consumers know the heuristic's limit.
    """

    ticker: str
    call_bto_volume: int
    call_sto_volume: int
    put_bto_volume: int
    put_sto_volume: int
    attribution_method: str


def _select_latest_snapshot_at(
    session: Session,
    *,
    contract_ticker: str,
    as_of: str,
) -> OptionsContractSnapshots | None:
    """Return the snapshot at exactly ``as_of`` for ``contract_ticker``."""
    stmt = select(OptionsContractSnapshots).where(
        OptionsContractSnapshots.contract_ticker == contract_ticker,
        OptionsContractSnapshots.snapshot_ts == as_of,
    )
    return session.execute(stmt).scalar_one_or_none()


def _select_prior_snapshot(
    session: Session,
    *,
    contract_ticker: str,
    as_of: str,
) -> OptionsContractSnapshots | None:
    """Return the most recent snapshot strictly before ``as_of``."""
    stmt = (
        select(OptionsContractSnapshots)
        .where(
            OptionsContractSnapshots.contract_ticker == contract_ticker,
            OptionsContractSnapshots.snapshot_ts < as_of,
        )
        .order_by(OptionsContractSnapshots.snapshot_ts.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _select_contracts_for_underlying(
    session: Session,
    *,
    underlying: str,
) -> list[OptionsContracts]:
    """Return all option contracts on ``underlying`` ordered by contract_ticker."""
    stmt = (
        select(OptionsContracts)
        .where(OptionsContracts.underlying_ticker == underlying)
        .order_by(OptionsContracts.contract_ticker)
    )
    return list(session.execute(stmt).scalars().all())


def classify_options_flow(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
) -> dict[str, TickerOptionsFlow]:
    """Aggregate today's options flow by underlying using the OI-delta heuristic.

    For each ticker in ``ticker_scope``: enumerate every contract on the
    underlying, read the snapshot at ``as_of`` and the most recent prior
    snapshot, classify today's ``volume_today`` as BTO when the OI rose and
    STO when the OI flat-or-decreased, and split by ``contract_type``
    (``call`` / ``put``).

    Contracts with a missing ``volume_today`` are skipped — no flow to
    attribute. Contracts with no prior snapshot are treated as opening: the
    first observation of OI is itself the rise from zero.
    """
    out: dict[str, TickerOptionsFlow] = {}
    for ticker in ticker_scope:
        # Per-bucket counters keyed by (contract_type, "bto"|"sto"). Flattening
        # the call/put by opening/closing dispatch into a single addressable map
        # avoids a four-arm nested-conditional cascade per contract.
        buckets: dict[tuple[str, str], int] = {
            ("call", "bto"): 0,
            ("call", "sto"): 0,
            ("put", "bto"): 0,
            ("put", "sto"): 0,
        }
        for contract in _select_contracts_for_underlying(session, underlying=ticker):
            snapshot = _select_latest_snapshot_at(
                session, contract_ticker=contract.contract_ticker, as_of=as_of
            )
            if snapshot is None or snapshot.volume_today is None:
                continue
            volume = int(snapshot.volume_today)
            if volume == 0:
                continue
            prior = _select_prior_snapshot(
                session, contract_ticker=contract.contract_ticker, as_of=as_of
            )
            prior_oi = (
                int(prior.open_interest)
                if prior is not None and prior.open_interest is not None
                else 0
            )
            current_oi = (
                int(snapshot.open_interest) if snapshot.open_interest is not None else prior_oi
            )
            side = "bto" if current_oi > prior_oi else "sto"
            buckets[(contract.contract_type, side)] += volume
        out[ticker] = TickerOptionsFlow(
            ticker=ticker,
            call_bto_volume=buckets[("call", "bto")],
            call_sto_volume=buckets[("call", "sto")],
            put_bto_volume=buckets[("put", "bto")],
            put_sto_volume=buckets[("put", "sto")],
            attribution_method=SNAPSHOT_OI_DELTA_ATTRIBUTION,
        )
    return out


# ---------------------------------------------------------------------------
# Protective vs. speculative tagging — quant 3f
# ---------------------------------------------------------------------------


PutFlowIntent = Literal["protective", "speculative"]
"""Two-value taxonomy for put-flow intent.

Per ``external.md`` § 2: a put on a name where the system holds long reads
as hedging (``protective``); otherwise it reads as bearish conviction
(``speculative``). The story-level v1 simplification reduces the broader
"known institutional ownership" signal to "system's own holdings via the
positions table"; downstream stories can refine the protective gate.
"""


def classify_put_flow_intent(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    system_long_positions: Mapping[str, int],
    protective_holding_pct_of_adv: float,
) -> dict[str, PutFlowIntent]:
    """Tag each ticker's put flow as ``protective`` or ``speculative``.

    A ticker reads as ``protective`` when ``system_long_positions[ticker]``
    is at least ``protective_holding_pct_of_adv`` percent of the ticker's
    ``asset_universe.avg_daily_volume_shares``. Tickers absent from the
    holdings map default to ``speculative``.

    The simplification (v1): the only system-side ownership signal available
    is the system's own long holdings. Known institutional ownership
    inference from ``asset_universe.float_shares`` is too noisy for a binary
    protective/speculative split — defer the broader heuristic per the
    story-08b Notes section.

    Tickers missing from ``asset_universe`` or whose ADV is non-positive
    default to ``speculative`` because the threshold can't be evaluated.
    """
    out: dict[str, PutFlowIntent] = {}
    for ticker in ticker_scope:
        held_shares = int(system_long_positions.get(ticker, 0))
        if held_shares <= 0:
            out[ticker] = "speculative"
            continue
        adv_stmt = select(AssetUniverse.avg_daily_volume_shares).where(
            AssetUniverse.ticker == ticker
        )
        adv = session.execute(adv_stmt).scalar_one_or_none()
        if adv is None or adv <= 0:
            out[ticker] = "speculative"
            continue
        held_pct = (held_shares / float(adv)) * 100.0
        if held_pct >= protective_holding_pct_of_adv:
            out[ticker] = "protective"
        else:
            out[ticker] = "speculative"
    return out


# ---------------------------------------------------------------------------
# Index hedging vs. sector conviction classification — quant 3h
# ---------------------------------------------------------------------------


IndexVsSectorLabel = Literal[
    "macro_hedging",
    "sector_specific_concern",
    "index_hedging_no_sector_view",
]
"""Three-label taxonomy distinguishing macro from sector-specific hedging.

Per ``external.md`` § 2: when SPY/QQQ put flow and sector-ETF put flow both
spike, the distinguishing question is whether the bet is on the index or
the sector. The three labels cover the three observable conditions; absence
of either spike is the no-finding case.
"""


@dataclass(frozen=True, slots=True)
class IndexVsSectorClassification:
    """One ``index_vs_sector_classification`` finding."""

    label: IndexVsSectorLabel
    index_max_put_z: float
    sector_max_put_z: float


def classify_index_vs_sector_flow(
    *,
    index_flow_zscores: Mapping[str, FlowZScore],
    sector_etf_flow_zscores: Mapping[str, FlowZScore],
    sigma_threshold: float,
) -> IndexVsSectorClassification | None:
    """Classify the relationship between index and sector-ETF put flow.

    Returns ``None`` when neither side has any put-BTO z-score above
    ``sigma_threshold``. Otherwise the three labels:

    - ``macro_hedging`` when both sides spike.
    - ``sector_specific_concern`` when sector-ETF puts spike but index puts do not.
    - ``index_hedging_no_sector_view`` when index puts spike but sector-ETF puts do not.

    The per-side magnitudes carried in the result are the maxima across the
    side — operators reading the finding want the strongest signal, not an
    average.
    """
    index_max = max(
        (score.put_bto_z for score in index_flow_zscores.values()),
        default=0.0,
    )
    sector_max = max(
        (score.put_bto_z for score in sector_etf_flow_zscores.values()),
        default=0.0,
    )
    index_spike = index_max >= sigma_threshold
    sector_spike = sector_max >= sigma_threshold
    if not index_spike and not sector_spike:
        return None
    label: IndexVsSectorLabel
    if index_spike and sector_spike:
        label = "macro_hedging"
    elif sector_spike:
        label = "sector_specific_concern"
    else:
        label = "index_hedging_no_sector_view"
    return IndexVsSectorClassification(
        label=label,
        index_max_put_z=index_max,
        sector_max_put_z=sector_max,
    )
