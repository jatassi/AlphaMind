"""Tests for q1 loaders + the DistillationRepository(Protocol).

The loaders are the IO shell; tests here exercise the public Protocol via
an in-memory dict-backed stub so the contract is independent of SQLAlchemy.
"""

from __future__ import annotations

from collections.abc import Sequence

from alphamind._kernel.ids import Symbol
from alphamind.distillation._repository import (
    ContractCurrentStateRow,
    ContractHistoryEntry,
    ContractMetadataRow,
    DailyBarRow,
    DistillationRepository,
    GapEventCounts,
    NewsLabelCountsRow,
    OptionsContractRow,
    OptionsContractSnapshotRow,
    SectorClassificationRow,
    TickerADVRow,
    TickerBaselineRow,
)


class _StubDistillationRepository:
    """Minimal in-memory test double structurally satisfying the Protocol.

    Does not inherit from :class:`DistillationRepository` so mypy's
    abstract-method check (the Protocol's empty-body methods become
    implicitly abstract under inheritance) doesn't apply. Structural
    conformance is asserted via :func:`_assert_is_repository`.
    """

    def __init__(
        self,
        *,
        bars: dict[str, Sequence[DailyBarRow]] | None = None,
        sectors: dict[str, SectorClassificationRow] | None = None,
        baselines: dict[tuple[str, str], TickerBaselineRow] | None = None,
        gap_fill_history: dict[str, GapEventCounts] | None = None,
        sector_gap_history: dict[str, GapEventCounts] | None = None,
        default_universe: Sequence[str] = (),
    ) -> None:
        self._bars = bars or {}
        self._sectors = sectors or {}
        self._baselines = baselines or {}
        self._gap_fill = gap_fill_history or {}
        self._sector_gap = sector_gap_history or {}
        self._default_universe = tuple(default_universe)

    def load_default_ticker_scope(self) -> tuple[str, ...]:
        return self._default_universe

    def load_sector_classifications(
        self, *, tickers: Sequence[str]
    ) -> dict[str, SectorClassificationRow]:
        return {t: self._sectors[t] for t in tickers if t in self._sectors}

    def load_daily_bars(self, *, ticker: str, as_of: str, days: int) -> tuple[DailyBarRow, ...]:
        bars = tuple(self._bars.get(ticker, ()))
        return bars[-days:] if days > 0 else bars

    def load_latest_baseline(
        self, *, ticker: str, kind: str, as_of: str
    ) -> TickerBaselineRow | None:
        return self._baselines.get((ticker, kind))

    def load_gap_fill_event_counts(self, *, ticker: str, as_of: str) -> GapEventCounts:
        return self._gap_fill.get(ticker, GapEventCounts(resolved=0, filled=0, pending=0))

    def load_sector_pooled_gap_fill_counts(self, *, sector: str, as_of: str) -> GapEventCounts:
        return self._sector_gap.get(sector, GapEventCounts(resolved=0, filled=0, pending=0))

    # q3 surface — stubbed empty for the q1-only tests in this module.
    def load_options_contracts_for_underlying(
        self, *, underlying: str
    ) -> tuple[OptionsContractRow, ...]:
        del underlying
        return ()

    def load_options_snapshot_pairs_for_underlying(
        self, *, underlying: str, as_of: str
    ) -> dict[str, tuple[OptionsContractSnapshotRow | None, OptionsContractSnapshotRow | None]]:
        del underlying, as_of
        return {}

    def load_ticker_adv(self, *, ticker: str) -> TickerADVRow | None:
        del ticker
        return None

    # qualitative surface — stubbed empty for the q1-only tests in this module.
    def load_news_article_label_counts(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> NewsLabelCountsRow:
        del ticker, range_start, range_end
        return NewsLabelCountsRow(positive=0, negative=0, neutral=0, mixed=0)

    def load_news_article_sentiment_scores(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> tuple[float, int] | None:
        del ticker, range_start, range_end
        return None

    def load_hourly_window_price_change(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> float | None:
        del ticker, range_start, range_end
        return None

    def load_universe_pooled_sentiment_distribution(
        self, *, as_of: str
    ) -> tuple[float, float] | None:
        del as_of
        return None

    def load_contract_history(
        self, *, contract_id: str, range_start: str, range_end: str
    ) -> tuple[ContractHistoryEntry, ...]:
        del contract_id, range_start, range_end
        return ()

    def load_contract_current_state(
        self, *, contract_id: str, as_of: str
    ) -> ContractCurrentStateRow | None:
        del contract_id, as_of
        return None

    def load_contract_metadata(self, *, contract_id: str) -> ContractMetadataRow | None:
        del contract_id
        return None

    def load_contract_24h_volume_and_liquidity(
        self, *, contract_id: str, as_of: str
    ) -> tuple[float, float]:
        del contract_id, as_of
        return 0.0, 0.0


def _assert_is_repository(stub: _StubDistillationRepository) -> DistillationRepository:
    """Type-narrow the stub to the Protocol — mypy enforces structural conformance."""
    return stub


def test_repository_protocol_is_satisfied_by_stub() -> None:
    """Stub instance type-checks as a DistillationRepository at runtime."""
    stub = _StubDistillationRepository()
    # Structural narrowing — mypy verifies the stub matches every Protocol
    # method during static analysis; this runtime call exercises the cast.
    repo: DistillationRepository = _assert_is_repository(stub)
    assert repo.load_default_ticker_scope() == ()
    assert stub.load_sector_classifications(tickers=["AAPL"]) == {}
    assert stub.load_daily_bars(ticker=Symbol("AAPL"), as_of="x", days=5) == ()
    assert stub.load_latest_baseline(ticker=Symbol("AAPL"), kind="atr", as_of="x") is None
    counts = stub.load_gap_fill_event_counts(ticker=Symbol("AAPL"), as_of="x")
    assert counts.resolved == 0 and counts.filled == 0
    sector_counts = stub.load_sector_pooled_gap_fill_counts(sector="tech", as_of="x")
    assert sector_counts.resolved == 0 and sector_counts.filled == 0


def test_stub_returns_loaded_bars_in_chronological_order() -> None:
    """Stub honors the days=N tail slice the production loader implements."""
    bars = [
        DailyBarRow(
            ticker=Symbol("AAPL"),
            period_start=f"2026-04-{day:02d}T00:00:00Z",
            adj_open=100.0,
            adj_high=101.0,
            adj_low=99.0,
            adj_close=100.5,
            adj_volume=1_000_000,
        )
        for day in range(1, 11)
    ]
    stub = _StubDistillationRepository(bars={"AAPL": bars})
    loaded = stub.load_daily_bars(ticker=Symbol("AAPL"), as_of="x", days=3)
    assert len(loaded) == 3
    # Most recent three bars.
    assert loaded[-1].period_start == "2026-04-10T00:00:00Z"
    assert loaded[0].period_start == "2026-04-08T00:00:00Z"
