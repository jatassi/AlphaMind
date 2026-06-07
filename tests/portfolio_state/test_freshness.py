"""Tests for the freshness contract (story 08)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.freshness import (
    AssembledSnapshot,
    PriceFetchOutcomes,
    SnapshotFreshness,
    compute_snapshot_freshness,
)
from alphamind.portfolio_state.pricing import PriceQuote, PriceSource

from ._view_builders import (
    _make_open_position,
    _make_pending_position,
    _make_snapshot,
)

# ---------------------------------------------------------------------------
# Shared timestamps
# ---------------------------------------------------------------------------

_FILL_COLLECTION_AT = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
_NOW = datetime(2025, 6, 1, 9, 0, 30, tzinfo=UTC)  # 30s after fill_collection
_ENTRY_AT = datetime(2025, 6, 1, 8, 0, 0, tzinfo=UTC)
_PRICE_AS_OF = datetime(2025, 6, 1, 8, 59, 50, tzinfo=UTC)  # 40s before _NOW

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_config(
    max_fill_collection_to_snapshot_seconds: float = 300.0,
    max_price_age_seconds: float = 60.0,
) -> PortfolioStateConfig:
    return PortfolioStateConfig(
        pm_decision_log_sliding_window_invocations=5,
        thesis_resolutions_lookback_trading_days=10,
        thesis_quality_aggregates_trailing_windows_days=(5, 20),
        snapshot_freshness_max_fill_collection_to_snapshot_seconds=max_fill_collection_to_snapshot_seconds,
        snapshot_freshness_max_price_age_seconds=max_price_age_seconds,
        snapshot_freshness_max_option_price_age_seconds=max_price_age_seconds,
    )


def _make_fresh_quote(ticker: str, as_of: datetime = _PRICE_AS_OF) -> PriceQuote:
    return PriceQuote(
        ticker=ticker,
        price_usd=520.0,
        as_of_timestamp=as_of,
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,
    )


# ---------------------------------------------------------------------------
# PriceFetchOutcomes tests
# ---------------------------------------------------------------------------


def test_price_fetch_outcomes_happy_path() -> None:
    """Three disjoint sets construct successfully."""
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"POS-003"}),
        position_ids_unknown_ticker=frozenset({"POS-004"}),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    assert "POS-001" in outcomes.position_ids_priced_fresh
    assert "POS-003" in outcomes.position_ids_priced_stale
    assert "POS-004" in outcomes.position_ids_unknown_ticker


def test_price_fetch_outcomes_all_fresh() -> None:
    """All positions fresh, oldest_price_as_of set."""
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    assert outcomes.position_ids_priced_stale == frozenset()


def test_price_fetch_outcomes_no_positions() -> None:
    """Zero positions, oldest_price_as_of is None."""
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    assert outcomes.oldest_price_as_of is None


def test_price_fetch_outcomes_overlapping_fresh_stale_raises() -> None:
    """A position_id in fresh and stale sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset({"POS-001"}),
            position_ids_priced_stale=frozenset({"POS-001"}),
            position_ids_unknown_ticker=frozenset(),
            oldest_price_as_of=None,
        )


def test_price_fetch_outcomes_overlapping_fresh_unknown_raises() -> None:
    """A position_id in fresh and unknown sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset({"POS-001"}),
            position_ids_priced_stale=frozenset(),
            position_ids_unknown_ticker=frozenset({"POS-001"}),
            oldest_price_as_of=None,
        )


def test_price_fetch_outcomes_overlapping_stale_unknown_raises() -> None:
    """A position_id in stale and unknown sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset(),
            position_ids_priced_stale=frozenset({"POS-001"}),
            position_ids_unknown_ticker=frozenset({"POS-001"}),
            oldest_price_as_of=None,
        )


def test_price_fetch_outcomes_oldest_price_non_tz_aware_raises() -> None:
    """oldest_price_as_of must be tz-aware UTC."""
    # Strip tzinfo from a known tz-aware datetime to get a naive datetime for testing
    naive_dt = _FILL_COLLECTION_AT.replace(tzinfo=None)
    with pytest.raises((ValueError, TypeError)):
        PriceFetchOutcomes(
            position_ids_priced_fresh=frozenset(),
            position_ids_priced_stale=frozenset(),
            position_ids_unknown_ticker=frozenset(),
            oldest_price_as_of=naive_dt,
        )


# ---------------------------------------------------------------------------
# SnapshotFreshness tests
# ---------------------------------------------------------------------------


def _make_freshness(
    fill_collection_committed_at: datetime = _FILL_COLLECTION_AT,
    snapshot_assembled_at: datetime = _NOW,
    fill_collection_to_snapshot_seconds: float = 30.0,
    max_fill_collection_to_snapshot_seconds: float = 300.0,
    fill_collection_to_snapshot_within_threshold: bool = True,
    total_open_positions: int = 2,
    total_pending_positions: int = 1,
    total_positions: int = 3,
    position_ids_priced_fresh: frozenset[str] = frozenset({"POS-001", "POS-002"}),
    position_ids_priced_stale: frozenset[str] = frozenset({"PEND-001"}),
    position_ids_unknown_ticker: frozenset[str] = frozenset(),
    count_priced_fresh: int = 2,
    count_priced_stale: int = 1,
    count_unknown_ticker: int = 0,
    all_position_prices_fresh: bool = False,
    oldest_price_as_of: datetime | None = _PRICE_AS_OF,
    oldest_price_age_seconds: float | None = 40.0,
    max_price_age_seconds: float = 60.0,
) -> SnapshotFreshness:
    return SnapshotFreshness(
        fill_collection_committed_at=fill_collection_committed_at,
        snapshot_assembled_at=snapshot_assembled_at,
        fill_collection_to_snapshot_seconds=fill_collection_to_snapshot_seconds,
        max_fill_collection_to_snapshot_seconds=max_fill_collection_to_snapshot_seconds,
        fill_collection_to_snapshot_within_threshold=fill_collection_to_snapshot_within_threshold,
        total_open_positions=total_open_positions,
        total_pending_positions=total_pending_positions,
        total_positions=total_positions,
        position_ids_priced_fresh=position_ids_priced_fresh,
        position_ids_priced_stale=position_ids_priced_stale,
        position_ids_unknown_ticker=position_ids_unknown_ticker,
        count_priced_fresh=count_priced_fresh,
        count_priced_stale=count_priced_stale,
        count_unknown_ticker=count_unknown_ticker,
        all_position_prices_fresh=all_position_prices_fresh,
        oldest_price_as_of=oldest_price_as_of,
        oldest_price_age_seconds=oldest_price_age_seconds,
        max_price_age_seconds=max_price_age_seconds,
    )


def test_snapshot_freshness_happy_path() -> None:
    """Build SnapshotFreshness with known fixture; verify all fields match."""
    sf = _make_freshness()
    assert sf.fill_collection_committed_at == _FILL_COLLECTION_AT
    assert sf.snapshot_assembled_at == _NOW
    assert sf.fill_collection_to_snapshot_seconds == pytest.approx(30.0)
    assert sf.max_fill_collection_to_snapshot_seconds == pytest.approx(300.0)
    assert sf.fill_collection_to_snapshot_within_threshold is True
    assert sf.total_open_positions == 2
    assert sf.total_pending_positions == 1
    assert sf.total_positions == 3
    assert sf.count_priced_fresh == 2
    assert sf.count_priced_stale == 1
    assert sf.count_unknown_ticker == 0
    assert sf.all_position_prices_fresh is False
    assert sf.oldest_price_as_of == _PRICE_AS_OF
    assert sf.oldest_price_age_seconds == pytest.approx(40.0)


def test_snapshot_freshness_count_conservation_violated_raises() -> None:
    """count_priced_fresh + stale + unknown != total_positions raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            total_positions=3,
            count_priced_fresh=2,
            count_priced_stale=0,
            count_unknown_ticker=0,  # 2 + 0 + 0 = 2 != 3
        )


def test_snapshot_freshness_disjoint_sets_violated_raises() -> None:
    """Position ID in two sets raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            position_ids_priced_fresh=frozenset({"POS-001", "SHARED"}),
            position_ids_priced_stale=frozenset({"SHARED"}),
            position_ids_unknown_ticker=frozenset(),
            count_priced_fresh=2,
            count_priced_stale=1,
            count_unknown_ticker=0,
            total_positions=3,
        )


def test_snapshot_freshness_negative_fill_collection_to_snapshot_raises() -> None:
    """fill_collection_to_snapshot_seconds < 0 raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            fill_collection_to_snapshot_seconds=-1.0,
        )


def test_snapshot_freshness_oldest_price_after_assembled_raises() -> None:
    """oldest_price_as_of > snapshot_assembled_at raises ValidationError."""
    future = _NOW + timedelta(seconds=10)
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            oldest_price_as_of=future,
            oldest_price_age_seconds=-10.0,  # won't get here — validator fires first
        )


def test_snapshot_freshness_oldest_price_age_none_iff_no_positions() -> None:
    """oldest_price_age_seconds is None iff oldest_price_as_of is None."""
    # Both None — valid (no positions)
    sf_no_pos = _make_freshness(
        total_open_positions=0,
        total_pending_positions=0,
        total_positions=0,
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        count_priced_fresh=0,
        count_priced_stale=0,
        count_unknown_ticker=0,
        all_position_prices_fresh=True,
        oldest_price_as_of=None,
        oldest_price_age_seconds=None,
    )
    assert sf_no_pos.oldest_price_age_seconds is None

    # oldest_price_as_of is None but oldest_price_age_seconds is not None — invalid
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            total_open_positions=0,
            total_pending_positions=0,
            total_positions=0,
            position_ids_priced_fresh=frozenset(),
            position_ids_priced_stale=frozenset(),
            position_ids_unknown_ticker=frozenset(),
            count_priced_fresh=0,
            count_priced_stale=0,
            count_unknown_ticker=0,
            all_position_prices_fresh=True,
            oldest_price_as_of=None,
            oldest_price_age_seconds=5.0,
        )


def test_snapshot_freshness_total_positions_mismatch_raises() -> None:
    """total_positions != total_open + total_pending raises ValidationError."""
    with pytest.raises((ValueError, TypeError)):
        _make_freshness(
            total_open_positions=2,
            total_pending_positions=1,
            total_positions=99,  # wrong
        )


# ---------------------------------------------------------------------------
# SnapshotFreshness.is_position_price_stale tests
# ---------------------------------------------------------------------------


def test_is_position_price_stale_fresh_returns_false() -> None:
    sf = _make_freshness(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"PEND-001"}),
        position_ids_unknown_ticker=frozenset(),
    )
    assert sf.is_position_price_stale("POS-001") is False
    assert sf.is_position_price_stale("POS-002") is False


def test_is_position_price_stale_stale_returns_true() -> None:
    sf = _make_freshness(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"PEND-001"}),
        position_ids_unknown_ticker=frozenset(),
    )
    assert sf.is_position_price_stale("PEND-001") is True


def test_is_position_price_stale_unknown_ticker_returns_true() -> None:
    sf = _make_freshness(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset({"PEND-001"}),
        count_priced_stale=0,
        count_unknown_ticker=1,
        all_position_prices_fresh=False,
    )
    assert sf.is_position_price_stale("PEND-001") is True


def test_is_position_price_stale_unknown_position_id_returns_false() -> None:
    """Unknown position ID (not in any set) returns False — no exception."""
    sf = _make_freshness()
    assert sf.is_position_price_stale("POS-DOES-NOT-EXIST") is False


# ---------------------------------------------------------------------------
# SnapshotFreshness.staleness_summary tests
# ---------------------------------------------------------------------------


def test_staleness_summary_all_fresh() -> None:
    sf = _make_freshness(
        total_open_positions=2,
        total_pending_positions=1,
        total_positions=3,
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002", "PEND-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        count_priced_fresh=3,
        count_priced_stale=0,
        count_unknown_ticker=0,
        all_position_prices_fresh=True,
        fill_collection_to_snapshot_seconds=2.3,
        max_fill_collection_to_snapshot_seconds=30.0,
        oldest_price_as_of=_PRICE_AS_OF,
        oldest_price_age_seconds=40.0,
    )
    summary = sf.staleness_summary()
    assert summary == "fill_collection→snapshot 2.3s (within 30.0s); 3/3 positions priced fresh"


def test_staleness_summary_mixed() -> None:
    sf = _make_freshness(
        total_open_positions=2,
        total_pending_positions=1,
        total_positions=3,
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002"}),
        position_ids_priced_stale=frozenset({"PEND-001"}),
        position_ids_unknown_ticker=frozenset(),
        count_priced_fresh=2,
        count_priced_stale=1,
        count_unknown_ticker=0,
        all_position_prices_fresh=False,
        fill_collection_to_snapshot_seconds=2.3,
        max_fill_collection_to_snapshot_seconds=30.0,
        oldest_price_as_of=_PRICE_AS_OF,
        oldest_price_age_seconds=40.0,
    )
    summary = sf.staleness_summary()
    assert "2/3 positions priced fresh" in summary
    assert "1 stale" in summary
    assert "PEND-001" in summary


# ---------------------------------------------------------------------------
# fill_collection_to_snapshot_within_threshold boundary tests
# ---------------------------------------------------------------------------


def test_fill_collection_to_snapshot_within_threshold_exactly_at_boundary() -> None:
    """Exactly at max returns True."""
    sf = _make_freshness(
        fill_collection_to_snapshot_seconds=300.0,
        max_fill_collection_to_snapshot_seconds=300.0,
        fill_collection_to_snapshot_within_threshold=True,
    )
    assert sf.fill_collection_to_snapshot_within_threshold is True


def test_fill_collection_to_snapshot_within_threshold_1ms_beyond() -> None:
    """1ms beyond max returns False."""
    sf = _make_freshness(
        fill_collection_to_snapshot_seconds=300.001,
        max_fill_collection_to_snapshot_seconds=300.0,
        fill_collection_to_snapshot_within_threshold=False,
    )
    assert sf.fill_collection_to_snapshot_within_threshold is False


# ---------------------------------------------------------------------------
# compute_snapshot_freshness tests
# ---------------------------------------------------------------------------


def test_compute_snapshot_freshness_happy_path() -> None:
    """2 open + 1 pending positions, all fresh — produces expected SnapshotFreshness."""
    pos1 = _make_open_position("POS-001", "NVDA")
    pos2 = _make_open_position("POS-002", "AAPL")
    pend = _make_pending_position("PEND-001", "MSFT")
    snapshot = _make_snapshot(
        open_positions=(pos1, pos2),
        pending_positions=(pend,),
        fill_collection_committed_at=_FILL_COLLECTION_AT,
        snapshot_assembled_at=_NOW,
    )
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001", "POS-002", "PEND-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    config = _make_config(
        max_fill_collection_to_snapshot_seconds=300.0,
        max_price_age_seconds=60.0,
    )

    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=config)

    assert freshness.total_open_positions == 2
    assert freshness.total_pending_positions == 1
    assert freshness.total_positions == 3
    assert freshness.count_priced_fresh == 3
    assert freshness.count_priced_stale == 0
    assert freshness.count_unknown_ticker == 0
    assert freshness.all_position_prices_fresh is True
    # fill_collection = _FILL_COLLECTION_AT, assembled = _NOW = fill_collection + 30s
    assert freshness.fill_collection_to_snapshot_seconds == pytest.approx(30.0)
    assert freshness.fill_collection_to_snapshot_within_threshold is True
    # oldest_price_as_of = _PRICE_AS_OF = _NOW - 40s
    assert freshness.oldest_price_as_of == _PRICE_AS_OF
    assert freshness.oldest_price_age_seconds == pytest.approx(40.0)


def test_compute_snapshot_freshness_extra_position_id_in_outcomes_raises() -> None:
    """fetch_outcomes references a position_id not in snapshot → ValueError."""
    # empty (force; shared _make_snapshot defaults to rich data)
    snapshot = _make_snapshot(open_positions=(), pending_positions=())
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-DOES-NOT-EXIST"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    with pytest.raises(ValueError, match="position_id not in snapshot"):
        compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())


def test_compute_snapshot_freshness_missing_position_in_outcomes_raises() -> None:
    """Snapshot has a position not classified in fetch_outcomes → ValueError."""
    pos = _make_open_position("POS-001")
    snapshot = _make_snapshot(open_positions=(pos,))
    outcomes = PriceFetchOutcomes(  # POS-001 missing from all sets
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    with pytest.raises(ValueError, match="position_id not classified in fetch_outcomes"):
        compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())


def test_compute_snapshot_freshness_no_positions_oldest_price_age_none() -> None:
    """Zero positions → oldest_price_age_seconds is None."""
    # empty (force; shared _make_snapshot defaults to rich data)
    snapshot = _make_snapshot(open_positions=(), pending_positions=())
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())
    assert freshness.oldest_price_age_seconds is None
    assert freshness.oldest_price_as_of is None


def test_compute_snapshot_freshness_deterministic() -> None:
    """Identical inputs produce identical results across repeated calls."""
    pos = _make_open_position("POS-001")
    snapshot = _make_snapshot(
        open_positions=(pos,),
        fill_collection_committed_at=_FILL_COLLECTION_AT,
        snapshot_assembled_at=_NOW,
    )
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_PRICE_AS_OF,
    )
    config = _make_config()
    result1 = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=config)
    result2 = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=config)
    assert result1 == result2


def test_compute_snapshot_freshness_threshold_boundary() -> None:
    """Exactly at threshold: within=True; 1ms beyond: within=False."""
    pos = _make_open_position("POS-001")

    # Exactly at threshold: snapshot_assembled_at = fill_collection + 300s
    snapshot_exact = _make_snapshot(
        open_positions=(pos,),
        fill_collection_committed_at=_FILL_COLLECTION_AT,
        snapshot_assembled_at=_FILL_COLLECTION_AT + timedelta(seconds=300),
    )
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset({"POS-001"}),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=_FILL_COLLECTION_AT,
    )
    config = _make_config(max_fill_collection_to_snapshot_seconds=300.0)
    freshness_exact = compute_snapshot_freshness(
        snapshot_exact, fetch_outcomes=outcomes, config=config
    )
    assert freshness_exact.fill_collection_to_snapshot_within_threshold is True

    # 1ms beyond
    snapshot_beyond = _make_snapshot(
        open_positions=(pos,),
        fill_collection_committed_at=_FILL_COLLECTION_AT,
        snapshot_assembled_at=_FILL_COLLECTION_AT + timedelta(seconds=300, milliseconds=1),
    )
    freshness_beyond = compute_snapshot_freshness(
        snapshot_beyond, fetch_outcomes=outcomes, config=config
    )
    assert freshness_beyond.fill_collection_to_snapshot_within_threshold is False


# ---------------------------------------------------------------------------
# AssembledSnapshot tests
# ---------------------------------------------------------------------------


def test_assembled_snapshot_constructs() -> None:
    """AssembledSnapshot bundles snapshot, freshness, and price_map correctly."""
    snapshot = _make_snapshot(open_positions=(), pending_positions=())
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())
    assembled = AssembledSnapshot(snapshot=snapshot, freshness=freshness, price_map={})
    assert assembled.snapshot is snapshot
    assert assembled.freshness is freshness
    assert assembled.price_map == {}


def test_assembled_snapshot_is_frozen() -> None:
    """Mutating snapshot, freshness, or price_map raises an error."""
    snapshot = _make_snapshot(open_positions=(), pending_positions=())
    outcomes = PriceFetchOutcomes(
        position_ids_priced_fresh=frozenset(),
        position_ids_priced_stale=frozenset(),
        position_ids_unknown_ticker=frozenset(),
        oldest_price_as_of=None,
    )
    freshness = compute_snapshot_freshness(snapshot, fetch_outcomes=outcomes, config=_make_config())
    assembled = AssembledSnapshot(snapshot=snapshot, freshness=freshness, price_map={})
    with pytest.raises(FrozenInstanceError):
        assembled.snapshot = _make_snapshot()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        assembled.freshness = freshness  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        assembled.price_map = {}  # type: ignore[misc]
