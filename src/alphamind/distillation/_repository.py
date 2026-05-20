"""DistillationRepository(Protocol) — pilot for the compute/load boundary split.

Story ALP-467 introduces this Protocol as the seam between the IO shell
(``q*/<sub>_loaders.py``) and the pure compute cores (``q*/<sub>_compute.py``).
Concrete impl lives in :mod:`alphamind.distillation._repository_sql` and
closes over a SQLAlchemy ``Session``; pure-compute tests substitute an
in-memory stub.

The Protocol surface is **pilot-scoped**: it lists only the read methods the
piloted modules (q1 + q3/flow_classification) consume. Audit pre-resolved
decision (E) — "Repository Protocol propagation: pilot only" — explicitly
defers broader propagation to follow-up issues so this file is not a full
distillation read surface. Methods accrete here as compute modules surface
new read needs.

The frozen-dataclass row types below mirror the persistence ORM shape but
carry only the fields the compute layer reads. Computing over these
hand-loaded rows is what makes a pure-compute unit test possible without
spinning up SQLite.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Frozen-dataclass row types — compute-facing projections of ORM rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyBarRow:
    """One row from ``ohlcv_bars`` for the daily timeframe."""

    ticker: str
    period_start: str
    adj_open: float
    adj_high: float
    adj_low: float
    adj_close: float
    adj_volume: int


@dataclass(frozen=True, slots=True)
class SectorClassificationRow:
    """One row from ``sector_classification`` projected to the fields compute reads."""

    ticker: str
    alphamind_sector: str
    sector_etf: str


@dataclass(frozen=True, slots=True)
class TickerBaselineRow:
    """One row from ``distillation_ticker_baseline`` projected for compute."""

    ticker: str
    baseline_kind: str
    as_of: str
    mean: float
    stdev: float
    n_observations: int
    window_days: int
    calibration_state: str


@dataclass(frozen=True, slots=True)
class GapEventCounts:
    """Event totals for the gap-fill probability fallback chain.

    ``resolved`` counts events whose ``outcome`` is not the pending sentinel
    at or before ``as_of``; ``filled`` is the subset whose outcome is
    ``"filled"``; ``pending`` counts detected-but-not-yet-resolved events.
    The compute step combines these into a per-ticker rate, a sector-pooled
    fallback rate, or a calibration-state tag distinguishing "no events
    detected" from "events detected, awaiting outcome resolution" (ALP-573).
    """

    resolved: int
    filled: int
    pending: int


@dataclass(frozen=True, slots=True)
class OptionsContractRow:
    """One row from ``options_contracts``; q3 flow-classification consumer."""

    contract_ticker: str
    underlying_ticker: str
    contract_type: str  # "call" | "put"


@dataclass(frozen=True, slots=True)
class OptionsContractSnapshotRow:
    """One row from ``options_contract_snapshots``; q3 flow-classification consumer."""

    contract_ticker: str
    snapshot_ts: str
    volume_today: int | None
    open_interest: int | None


@dataclass(frozen=True, slots=True)
class TickerADVRow:
    """Per-ticker ADV projection; q3 put-flow-intent classification consumer."""

    ticker: str
    avg_daily_volume_shares: float | None


@dataclass(frozen=True, slots=True)
class NewsLabelCountsRow:
    """Vendor-sentiment label totals across articles in a window.

    Mirrors the per-(article, ticker) ``vendor_sentiment_label`` taxonomy
    from ``news_article_tickers`` rolled up to counts.
    """

    positive: int
    negative: int
    neutral: int
    mixed: int


@dataclass(frozen=True, slots=True)
class ContractHistoryEntry:
    """One trailing snapshot of ``distillation_contract_history`` for a contract."""

    snapshot_ts: str
    yes_probability: float


@dataclass(frozen=True, slots=True)
class ContractCurrentStateRow:
    """Latest ``distillation_contract_history`` row at-or-before ``as_of``."""

    yes_probability: float
    delta_pp_since_prior: float
    snapshot_ts: str


@dataclass(frozen=True, slots=True)
class ContractMetadataRow:
    """Static ``prediction_market_contracts`` projection for a contract.

    ``resolution_date`` is the raw ``Z``-suffixed ISO 8601 string from the
    contract row, or ``None`` when absent — the compute layer parses it via
    :func:`alphamind.distillation.qualitative.contract_freshness.parse_resolution_date`
    to anchor the implicit-year inference in past-date question detection.
    """

    platform: str
    description: str
    category: str
    resolution_date: str | None = None


# ---------------------------------------------------------------------------
# Protocol surface — read-only, structural typing
# ---------------------------------------------------------------------------


class DistillationRepository(Protocol):
    """Read seam between the IO shell and the pure compute cores.

    Methods are pilot-scoped (q1 + q3.flow_classification). Each call returns
    a frozen-dataclass projection of the underlying ORM row, never the ORM
    object itself — this keeps the compute consumer free of SQLAlchemy edges
    that the import-linter enforces.
    """

    # --- q1 universe / sector ---------------------------------------------

    def load_default_ticker_scope(self) -> tuple[str, ...]:
        """Default ticker scope: tickers in audience-covered alphamind_sectors."""
        ...

    def load_sector_classifications(
        self, *, tickers: Sequence[str]
    ) -> dict[str, SectorClassificationRow]:
        """Return ``{ticker: row}`` for the requested tickers."""
        ...

    # --- q1 bars / baselines ----------------------------------------------

    def load_daily_bars(self, *, ticker: str, as_of: str, days: int) -> tuple[DailyBarRow, ...]:
        """Daily bars in chronological order; tail-slice when ``days`` < total."""
        ...

    def load_latest_baseline(
        self, *, ticker: str, kind: str, as_of: str
    ) -> TickerBaselineRow | None:
        """Most recent ``distillation_ticker_baseline`` row at-or-before ``as_of``."""
        ...

    # --- q1 gap history ---------------------------------------------------

    def load_gap_fill_event_counts(self, *, ticker: str, as_of: str) -> GapEventCounts:
        """Per-ticker resolved/filled event counts at-or-before ``as_of``."""
        ...

    def load_sector_pooled_gap_fill_counts(self, *, sector: str, as_of: str) -> GapEventCounts:
        """Sector-pooled resolved/filled event counts."""
        ...

    # --- q3 flow classification -------------------------------------------

    def load_options_contracts_for_underlying(
        self, *, underlying: str
    ) -> tuple[OptionsContractRow, ...]:
        """All option contracts on ``underlying`` ordered by contract_ticker."""
        ...

    def load_options_snapshot_pairs_for_underlying(
        self, *, underlying: str, as_of: str
    ) -> Mapping[str, tuple[OptionsContractSnapshotRow | None, OptionsContractSnapshotRow | None]]:
        """Per-contract (today, prior) snapshot pair for every contract on ``underlying``.

        Returns one entry per contract that has at least one snapshot (today or
        prior); contracts with neither are absent. Today's snapshot is the row
        with ``snapshot_ts == as_of``; prior is the most recent row strictly
        before ``as_of``. The q3 loader composes the pair into
        :class:`PerContractSnapshotPair` — this protocol stays free of q3
        vocabulary.
        """
        ...

    def load_ticker_adv(self, *, ticker: str) -> TickerADVRow | None:
        """Per-ticker average-daily-volume projection."""
        ...

    # --- qualitative news --------------------------------------------------

    def load_news_article_label_counts(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> NewsLabelCountsRow:
        """Vendor-sentiment label totals for one ticker in ``[range_start, range_end]``."""
        ...

    def load_news_article_sentiment_scores(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> tuple[float, int] | None:
        """``(mean_score, n_articles)`` over per-(article, ticker) rows in window."""
        ...

    def load_hourly_window_price_change(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> float | None:
        """Signed ``last_close - first_open`` over 1h bars in window; ``None`` if empty."""
        ...

    def load_universe_pooled_sentiment_distribution(
        self, *, as_of: str
    ) -> tuple[float, float] | None:
        """Universe-wide ``(mean, stdev)`` over calibrated sentiment baselines at ``as_of``."""
        ...

    # --- qualitative prediction markets ------------------------------------

    def load_contract_history(
        self, *, contract_id: str, range_start: str, range_end: str
    ) -> tuple[ContractHistoryEntry, ...]:
        """``distillation_contract_history`` rows ascending in the trailing window."""
        ...

    def load_contract_current_state(
        self, *, contract_id: str, as_of: str
    ) -> ContractCurrentStateRow | None:
        """Latest ``distillation_contract_history`` row at-or-before ``as_of``."""
        ...

    def load_contract_metadata(self, *, contract_id: str) -> ContractMetadataRow | None:
        """Static ``prediction_market_contracts`` projection; ``None`` when absent."""
        ...

    def load_contract_24h_volume_and_liquidity(
        self, *, contract_id: str, as_of: str
    ) -> tuple[float, float]:
        """``(volume_24h_usd, liquidity_usd)`` from latest snapshot at-or-before ``as_of``.

        Defaults each to ``0.0`` when the column is NULL or no snapshot exists.
        """
        ...


__all__ = [
    "ContractCurrentStateRow",
    "ContractHistoryEntry",
    "ContractMetadataRow",
    "DailyBarRow",
    "DistillationRepository",
    "GapEventCounts",
    "NewsLabelCountsRow",
    "OptionsContractRow",
    "OptionsContractSnapshotRow",
    "SectorClassificationRow",
    "TickerADVRow",
    "TickerBaselineRow",
]
