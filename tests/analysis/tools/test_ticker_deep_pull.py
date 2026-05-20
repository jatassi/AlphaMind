"""Tests for ticker_deep_pull tool — ALP-260.

Covers all seven acceptance criteria:
  AC1  — module exports the required names
  AC2  — import resolves cleanly
  AC3  — TickerDeepPullOutput extends ToolEnvelope
  AC4  — all four categories requested → all payloads non-None, quality=COMPLETE
  AC5  — single category (price_volume) → others None
  AC6  — ticker not in AssetUniverse → quality=UNAVAILABLE, all payloads None
  AC7  — unknown category silently skipped
  AC8  — empty categories → quality=UNAVAILABLE
  AC9  — no magic numeric thresholds (structural: verified by module inspection)
  AC10 — __init__.py / _sdk_adapter.py untouched (git-level check, not a pytest test)
  AC11 — each AC covered by a test and passes
  AC12 — ruff/mypy clean

Partial-data tests:
  PD1  — ticker in universe but no ShortInterestSnapshot → short_data all-None fields,
          quality=PARTIAL

Determinism test:
  DET  — two calls against same fixture → byte-equal payloads
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401  — register InvocationRow on Base
from alphamind._kernel.ids import Symbol
from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality
from alphamind.analysis.tools.ticker_deep_pull import (
    TickerDeepPullCategory,
    TickerDeepPullInput,
    TickerDeepPullOutput,
    ticker_deep_pull_factory,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    BorrowCostDaily,
    EarningsEstimateRevisions,
    EarningsEventDetails,
    EventCalendar,
    MacroObservations,
    OhlcvBars,
    ShortInterestSnapshot,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


class _FixedClock:
    """Returns ``_NOW`` deterministically — pins the tool's age computation so
    a day-boundary race (module load vs. RealClock at call time) can't flip
    staleness tests near midnight UTC.
    """

    def now(self) -> datetime:
        return _NOW


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _add_ticker(
    session: Session,
    ticker: str,
    *,
    is_active: int = 1,
    removed_date: str | None = None,
    removal_reason: str | None = None,
) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Corp",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=is_active,
            added_date="2026-01-01",
            removed_date=removed_date,
            removal_reason=removal_reason,
            last_updated="2026-01-01T00:00:00Z",
        )
    )
    session.flush()


def _add_ohlcv_bars(
    session: Session,
    ticker: str,
    num_days: int = 21,
    *,
    latest_age_days: int = 1,
) -> None:
    """Add ``num_days`` daily OHLCV bars; the most recent bar is ``latest_age_days``
    days before ``_NOW`` (default 1 ≈ yesterday)."""
    base = _NOW - timedelta(days=num_days + latest_age_days - 1)
    ingested = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(num_days):
        day = base + timedelta(days=i)
        day_str = day.strftime("%Y-%m-%dT00:00:00Z")
        end_str = day.strftime("%Y-%m-%dT23:59:59Z")
        close = 100.0 + i * 0.5  # gently rising
        volume = 1_000_000 + i * 10_000
        session.add(
            OhlcvBars(
                ticker=ticker,
                timeframe="1d",
                period_start=day_str,
                period_end=end_str,
                session="regular",
                adj_open=close - 0.1,
                adj_high=close + 0.5,
                adj_low=close - 0.5,
                adj_close=close,
                adj_volume=volume,
                adj_vwap=close,
                unadj_open=close - 0.1,
                unadj_high=close + 0.5,
                unadj_low=close - 0.5,
                unadj_close=close,
                unadj_volume=volume,
                unadj_vwap=close,
                source="test",
                ingested_at=ingested,
            )
        )
    session.flush()


def _add_short_data(session: Session, ticker: str) -> None:
    """Add a ShortInterestSnapshot and BorrowCostDaily row for ticker."""
    ingested = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = _NOW.strftime("%Y-%m-%d")
    prior = (_NOW - timedelta(days=30)).strftime("%Y-%m-%d")

    session.add(
        ShortInterestSnapshot(
            settlement_date=today,
            ticker=ticker,
            current_short_shares=5_000_000,
            previous_short_shares=4_500_000,
            avg_daily_volume_shares=10_000_000,
            days_to_cover=2.5,
            change_pct=11.1,
            source="test",
            ingested_at=ingested,
        )
    )
    # older snapshot for 30d change
    session.add(
        ShortInterestSnapshot(
            settlement_date=prior,
            ticker=ticker,
            current_short_shares=4_000_000,
            previous_short_shares=3_800_000,
            avg_daily_volume_shares=10_000_000,
            days_to_cover=2.0,
            change_pct=5.0,
            source="test",
            ingested_at=ingested,
        )
    )
    session.add(
        BorrowCostDaily(
            observation_date=today,
            ticker=ticker,
            fee_pct=1.5,
            rebate_pct=None,
            available_shares=500_000,
            source="test",
            ingested_at=ingested,
        )
    )
    session.flush()


def _add_earnings_event(session: Session, ticker: str) -> None:
    ingested = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    reported_at = (_NOW - timedelta(days=10)).strftime("%Y-%m-%dT16:00:00Z")
    event_id = f"ev-{ticker.lower()}"
    session.add(
        EventCalendar(
            event_id=event_id,
            event_type="earnings",
            ticker=ticker,
            scheduled_at=reported_at,
            description=f"{ticker} earnings",
            status="completed",
            source="test",
            ingested_at=ingested,
            last_updated=ingested,
            sectors=None,
        )
    )
    session.flush()
    session.add(
        EarningsEventDetails(
            event_id=event_id,
            ticker=ticker,
            fiscal_period="Q1",
            fiscal_year=2026,
            eps_consensus=1.2,
            eps_actual=1.5,
            revenue_consensus_usd=4_000_000_000.0,
            revenue_actual_usd=4_200_000_000.0,
            reported_at=reported_at,
            source="test",
        )
    )
    session.flush()
    # one upward revision in last 30d
    revision_ts = (_NOW - timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")
    session.add(
        EarningsEstimateRevisions(
            revised_at=revision_ts,
            ticker=ticker,
            fiscal_year=2026,
            fiscal_period="Q2",
            metric="eps",
            consensus_value=1.3,
            prior_consensus_value=1.2,
            source="test",
            ingested_at=revision_ts,
        )
    )
    session.flush()


def _add_macro_data(session: Session) -> None:
    """Add treasury yield observations."""
    ingested = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = _NOW.strftime("%Y-%m-%d")
    session.add(
        MacroObservations(
            source="FRED",
            series_id="DGS2",
            observation_date=today,
            revision_number=0,
            value=4.5,
            units="percent",
            frequency="daily",
            ingested_at=ingested,
        )
    )
    session.add(
        MacroObservations(
            source="FRED",
            series_id="DGS10",
            observation_date=today,
            revision_number=0,
            value=4.2,
            units="percent",
            frequency="daily",
            ingested_at=ingested,
        )
    )
    session.flush()


def _populate_all(session: Session, ticker: str = "AAPL") -> None:
    """Populate all four categories for ``ticker``."""
    _add_ticker(session, ticker)
    _add_ohlcv_bars(session, ticker)
    _add_short_data(session, ticker)
    _add_earnings_event(session, ticker)
    _add_macro_data(session)
    session.commit()


# ---------------------------------------------------------------------------
# AC1: module exports required names
# ---------------------------------------------------------------------------


def test_exports_required_names() -> None:
    """AC1: All required names are importable from the module."""
    import alphamind.analysis.tools.ticker_deep_pull as mod

    for name in (
        "TickerDeepPullInput",
        "TickerDeepPullOutput",
        "TickerDeepPullCategory",
        "PriceVolumePayload",
        "ShortDataPayload",
        "EarningsPayload",
        "MacroContextPayload",
        "ticker_deep_pull_factory",
    ):
        assert hasattr(mod, name), f"Missing export: {name}"


# ---------------------------------------------------------------------------
# AC3: TickerDeepPullOutput extends ToolEnvelope
# ---------------------------------------------------------------------------


def test_output_extends_tool_envelope() -> None:
    """AC3: TickerDeepPullOutput is a subclass of ToolEnvelope."""
    assert issubclass(TickerDeepPullOutput, ToolEnvelope)


# ---------------------------------------------------------------------------
# AC4: all four categories → all payloads non-None, quality=COMPLETE
# ---------------------------------------------------------------------------


def test_all_four_categories_happy_path(session: Session) -> None:
    """AC4: all four categories requested → all payloads non-None, quality=COMPLETE."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(
        TickerDeepPullInput(
            ticker=Symbol("AAPL"),
            categories=(
                "price_volume",
                "short_data",
                "earnings",
                "macro_context",
            ),
        )
    )

    assert result.quality == ToolQuality.COMPLETE
    assert result.price_volume is not None
    assert result.short_data is not None
    assert result.earnings is not None
    assert result.macro_context is not None


def test_price_volume_payload_fields(session: Session) -> None:
    """price_volume payload carries expected fields with plausible values."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("price_volume",)))

    pv = result.price_volume
    assert pv is not None
    assert pv.last_close is not None and pv.last_close > 0
    assert pv.last_volume is not None and pv.last_volume > 0
    assert pv.avg_volume_20d is not None and pv.avg_volume_20d > 0
    assert pv.volume_vs_avg_ratio is not None


def test_short_data_payload_fields(session: Session) -> None:
    """short_data payload carries expected fields with plausible values."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("short_data",)))

    sd = result.short_data
    assert sd is not None
    assert sd.short_interest_pct is not None
    assert sd.borrow_rate_pct is not None
    assert sd.days_to_cover is not None


def test_earnings_payload_fields(session: Session) -> None:
    """earnings payload carries expected fields."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("earnings",)))

    ea = result.earnings
    assert ea is not None
    assert ea.most_recent_event_date is not None
    assert ea.eps_actual == pytest.approx(1.5)
    assert ea.eps_consensus == pytest.approx(1.2)
    assert ea.revisions_30d_count is not None and ea.revisions_30d_count >= 0


def test_macro_context_payload_fields(session: Session) -> None:
    """macro_context payload carries treasury yields."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("macro_context",)))

    mc = result.macro_context
    assert mc is not None
    assert mc.treasury_2y_yield == pytest.approx(4.5)
    assert mc.treasury_10y_yield == pytest.approx(4.2)
    # spread = 2y - 10y = 0.3
    assert mc.yield_curve_2_10_spread is not None


# ---------------------------------------------------------------------------
# AC5: single category → only that payload populated
# ---------------------------------------------------------------------------


def test_single_category_price_volume_only(session: Session) -> None:
    """AC5: only price_volume → other three fields are None."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("price_volume",)))

    assert result.price_volume is not None
    assert result.short_data is None
    assert result.earnings is None
    assert result.macro_context is None


# ---------------------------------------------------------------------------
# AC6: ticker not in AssetUniverse → UNAVAILABLE, all None
# ---------------------------------------------------------------------------


def test_unknown_ticker_returns_unavailable(session: Session) -> None:
    """AC6: ticker not in AssetUniverse → quality=UNAVAILABLE, all payloads None.

    The ticker-absent path stays distinct from the delisted/inactive path
    (ALP-585): a ticker that was never in the universe carries no ``reason``,
    so callers never mistake "unknown symbol" for "resolved corporate event".
    """
    session.commit()
    fn = ticker_deep_pull_factory(session)
    result = fn(
        TickerDeepPullInput(
            ticker=Symbol("ZZZZ"),
            categories=("price_volume", "short_data", "earnings", "macro_context"),
        )
    )

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.price_volume is None
    assert result.short_data is None
    assert result.earnings is None
    assert result.macro_context is None
    assert result.reason is None


# ---------------------------------------------------------------------------
# AC7: unknown category silently skipped
# ---------------------------------------------------------------------------


def test_unknown_category_silently_skipped(session: Session) -> None:
    """AC7: unknown category does not raise; result reflects only the known ones."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    # price_volume is valid; options_flow is unknown
    result = fn(
        TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("price_volume", "options_flow"))
    )

    # No raise; price_volume is populated; unknown is not present
    assert result.price_volume is not None
    # quality reflects only the valid category result
    assert result.quality != ToolQuality.UNAVAILABLE or result.price_volume is None


def test_only_unknown_categories_returns_unavailable(session: Session) -> None:
    """AC7 edge: only unknown categories → quality=UNAVAILABLE."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("options_flow",)))

    assert result.quality == ToolQuality.UNAVAILABLE


# ---------------------------------------------------------------------------
# AC8: empty categories → UNAVAILABLE
# ---------------------------------------------------------------------------


def test_empty_categories_returns_unavailable(session: Session) -> None:
    """AC8: categories=() → quality=UNAVAILABLE."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=()))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.price_volume is None
    assert result.short_data is None
    assert result.earnings is None
    assert result.macro_context is None


# ---------------------------------------------------------------------------
# PD1: partial data — no ShortInterestSnapshot
# ---------------------------------------------------------------------------


def test_partial_no_short_data_rows(session: Session) -> None:
    """PD1: ticker in universe, no ShortInterestSnapshot → short_data quality=PARTIAL;
    aggregate quality=PARTIAL.
    """
    _add_ticker(session, "MSFT")
    _add_ohlcv_bars(session, "MSFT")
    _add_earnings_event(session, "MSFT")
    _add_macro_data(session)
    session.commit()

    fn = ticker_deep_pull_factory(session)
    result = fn(
        TickerDeepPullInput(
            ticker=Symbol("MSFT"),
            categories=("price_volume", "short_data", "earnings", "macro_context"),
        )
    )

    # short_data was requested but no rows exist → payload non-None, all fields None
    assert result.short_data is not None
    assert result.short_data.short_interest_pct is None
    assert result.short_data.borrow_rate_pct is None
    assert result.short_data.days_to_cover is None
    # aggregate quality degrades because short_data had no data
    assert result.quality == ToolQuality.PARTIAL


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_output(session: Session) -> None:
    """DET: two calls with identical inputs return byte-equal payloads."""
    _populate_all(session)
    fn = ticker_deep_pull_factory(session)
    inp = TickerDeepPullInput(
        ticker=Symbol("AAPL"),
        categories=("price_volume", "short_data", "earnings", "macro_context"),
    )
    r1 = fn(inp)
    r2 = fn(inp)

    # Compare field by field excluding data_freshness (which calls datetime.now internally)
    assert r1.ticker == r2.ticker
    assert r1.quality == r2.quality
    assert r1.price_volume == r2.price_volume
    assert r1.short_data == r2.short_data
    assert r1.earnings == r2.earnings
    assert r1.macro_context == r2.macro_context


# ---------------------------------------------------------------------------
# TickerDeepPullCategory enum
# ---------------------------------------------------------------------------


def test_category_enum_values() -> None:
    """TickerDeepPullCategory has the four expected members."""
    values = {c.value for c in TickerDeepPullCategory}
    assert values == {"price_volume", "short_data", "earnings", "macro_context"}


# ---------------------------------------------------------------------------
# JSON schema shape
# ---------------------------------------------------------------------------


def test_output_schema_exposes_four_optional_category_fields() -> None:
    """TickerDeepPullOutput schema carries four Optional[<CategoryPayload>] fields."""
    schema = TickerDeepPullOutput.model_json_schema()
    props = schema.get("properties", {})
    for field in ("price_volume", "short_data", "earnings", "macro_context"):
        assert field in props, f"Schema missing field: {field}"


# ---------------------------------------------------------------------------
# ALP-539 — staleness surfacing
# ---------------------------------------------------------------------------


def test_price_volume_stale_when_latest_bar_old(session: Session) -> None:
    """Latest OHLCV bar older than the staleness threshold → quality=STALE
    with a non-null reason that names the category and identifies the latest
    bar's date.
    """
    _add_ticker(session, "CTRA")
    _add_ohlcv_bars(session, "CTRA", num_days=21, latest_age_days=11)
    session.commit()

    fn = ticker_deep_pull_factory(session, clock=_FixedClock())
    result = fn(TickerDeepPullInput(ticker=Symbol("CTRA"), categories=("price_volume",)))

    assert result.quality == ToolQuality.STALE
    assert result.reason is not None
    assert "price_volume" in result.reason
    # Last-known-good payload is preserved (the tool surfaces stale data, not None).
    assert result.price_volume is not None
    assert result.price_volume.last_close is not None


def test_price_volume_complete_when_latest_bar_fresh(session: Session) -> None:
    """Latest OHLCV bar within the threshold → quality=COMPLETE, reason=None."""
    _populate_all(session, "AAPL")
    fn = ticker_deep_pull_factory(session, clock=_FixedClock())
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("price_volume",)))

    assert result.quality == ToolQuality.COMPLETE
    assert result.reason is None


def test_price_volume_complete_at_threshold_boundary(session: Session) -> None:
    """Latest bar exactly at the threshold (5 calendar days) is NOT stale.
    Locks the `> threshold` (strict) semantics against accidental change to `>=`.
    """
    _add_ticker(session, "AAPL")
    _add_ohlcv_bars(session, "AAPL", num_days=21, latest_age_days=5)
    session.commit()

    fn = ticker_deep_pull_factory(session, clock=_FixedClock())
    result = fn(TickerDeepPullInput(ticker=Symbol("AAPL"), categories=("price_volume",)))

    assert result.quality == ToolQuality.COMPLETE
    assert result.reason is None


def test_stale_price_volume_propagates_reason_to_envelope(session: Session) -> None:
    """When price_volume is stale and other categories are fresh, the aggregate
    quality degrades to STALE and the envelope reason names the stale category.
    """
    _add_ticker(session, "CTRA")
    _add_ohlcv_bars(session, "CTRA", num_days=21, latest_age_days=11)
    _add_short_data(session, "CTRA")
    _add_earnings_event(session, "CTRA")
    _add_macro_data(session)
    session.commit()

    fn = ticker_deep_pull_factory(session, clock=_FixedClock())
    result = fn(
        TickerDeepPullInput(
            ticker=Symbol("CTRA"),
            categories=("price_volume", "short_data", "earnings", "macro_context"),
        )
    )

    assert result.quality == ToolQuality.STALE
    assert result.reason is not None
    assert "price_volume" in result.reason


def test_partial_no_bars_does_not_emit_stale_reason(session: Session) -> None:
    """Zero OHLCV rows → quality stays PARTIAL (not STALE), and reason is None —
    absence is not the same as staleness.
    """
    _add_ticker(session, "MSFT")
    # No OHLCV bars added; only the universe row exists.
    session.commit()

    fn = ticker_deep_pull_factory(session, clock=_FixedClock())
    result = fn(TickerDeepPullInput(ticker=Symbol("MSFT"), categories=("price_volume",)))

    assert result.quality == ToolQuality.PARTIAL
    assert result.reason is None


def test_output_schema_exposes_reason_field() -> None:
    """TickerDeepPullOutput schema carries a Optional[str] reason field."""
    schema = TickerDeepPullOutput.model_json_schema()
    assert "reason" in schema.get("properties", {})


# ---------------------------------------------------------------------------
# ALP-585 — delisted / inactive ticker short-circuit
# ---------------------------------------------------------------------------


def test_inactive_ticker_returns_unavailable_with_delisted_reason(session: Session) -> None:
    """A ticker present in asset_universe but is_active=0 short-circuits to
    quality=UNAVAILABLE with a reason naming removed_date and removal_reason.
    Per-category loaders are skipped: payloads stay None even though OHLCV bars
    exist for the ticker (a populated price_volume would prove the loader ran).
    """
    _add_ticker(
        session,
        "CTRA",
        is_active=0,
        removed_date="2026-05-07",
        removal_reason="acquired by DVN",
    )
    _add_ohlcv_bars(session, "CTRA", num_days=21, latest_age_days=11)
    session.commit()

    fn = ticker_deep_pull_factory(session, clock=_FixedClock())
    result = fn(
        TickerDeepPullInput(
            ticker=Symbol("CTRA"),
            categories=("price_volume", "short_data", "earnings", "macro_context"),
        )
    )

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.price_volume is None
    assert result.short_data is None
    assert result.earnings is None
    assert result.macro_context is None
    assert result.reason is not None
    assert "CTRA" in result.reason
    assert "2026-05-07" in result.reason
    assert "acquired by DVN" in result.reason


def test_inactive_ticker_with_null_removal_metadata_still_short_circuits(
    session: Session,
) -> None:
    """An inactive ticker whose removed_date / removal_reason were never stamped
    still short-circuits to quality=UNAVAILABLE with a usable reason — the
    fallbacks keep a raw ``None`` out of the agent-facing string.
    """
    _add_ticker(session, "DEAD", is_active=0)
    session.commit()

    fn = ticker_deep_pull_factory(session, clock=_FixedClock())
    result = fn(TickerDeepPullInput(ticker=Symbol("DEAD"), categories=("price_volume",)))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.price_volume is None
    assert result.reason is not None
    assert "DEAD" in result.reason
    assert "None" not in result.reason
