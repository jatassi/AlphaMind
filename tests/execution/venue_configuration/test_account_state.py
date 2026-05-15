"""Tests for venue_configuration.account_state — ALP-387.

Each test corresponds to one acceptance criterion from the user story.
Tests use public interfaces only: VenueAccountState and read_venue_account_state
importable from alphamind.execution.venue_configuration.
"""

from __future__ import annotations

import dataclasses
import datetime
from unittest.mock import MagicMock

import pytest

from alphamind.execution.broker_adapter.queries import TradeAccountSnapshot
from alphamind.execution.venue_configuration import (
    VenueAccountState,
    read_venue_account_state,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(**overrides: object) -> TradeAccountSnapshot:
    """Build a minimal TradeAccountSnapshot for tests."""
    defaults: dict[str, object] = {
        "account_id": "test-account-id",
        "cash": 10_000.0,
        "equity": 10_000.0,
        "buying_power": 20_000.0,
        "regt_buying_power": 20_000.0,
        "daytrading_buying_power": 40_000.0,
        "maintenance_margin": 2_500.0,
        "daytrade_count": 0,
        "pattern_day_trader": False,
        "status": "ACTIVE",
    }
    defaults.update(overrides)
    return TradeAccountSnapshot(**defaults)  # type: ignore[arg-type]


def _make_queries(snapshot: TradeAccountSnapshot) -> MagicMock:
    """Return a mock AccountStateQueries whose get_account() returns snapshot."""
    queries = MagicMock()
    queries.get_account.return_value = snapshot
    return queries


# ---------------------------------------------------------------------------
# 1. Importability
# ---------------------------------------------------------------------------


def test_venue_account_state_importable() -> None:
    """VenueAccountState is importable from the package root."""
    assert VenueAccountState is not None


def test_read_venue_account_state_importable() -> None:
    """read_venue_account_state is importable from the package root."""
    assert read_venue_account_state is not None


# ---------------------------------------------------------------------------
# 2. Basic construction — raw fields mirrored correctly
# ---------------------------------------------------------------------------


def test_read_returns_venue_account_state_instance() -> None:
    snapshot = _make_snapshot(equity=10_000.0, daytrade_count=0, pattern_day_trader=False)
    queries = _make_queries(snapshot)

    result = read_venue_account_state(queries)

    assert isinstance(result, VenueAccountState)


def test_raw_fields_mirrored() -> None:
    """All raw /v2/account fields appear unchanged in the snapshot."""
    snapshot = _make_snapshot(
        cash=5_000.0,
        equity=10_000.0,
        buying_power=20_000.0,
        regt_buying_power=18_000.0,
        daytrading_buying_power=40_000.0,
        maintenance_margin=2_500.0,
        daytrade_count=1,
        pattern_day_trader=False,
    )
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.cash == 5_000.0
    assert result.equity == 10_000.0
    assert result.buying_power == 20_000.0
    assert result.regt_buying_power == 18_000.0
    assert result.daytrading_buying_power == 40_000.0
    assert result.maintenance_margin == 2_500.0
    assert result.daytrade_count == 1
    assert result.pattern_day_trader is False


# ---------------------------------------------------------------------------
# 3. PDT-qualified account at $50k — standard tier
# ---------------------------------------------------------------------------


def test_pdt_qualified_50k_standard_tier() -> None:
    """equity=$50k, pattern_day_trader=True → pdt_qualified=True, headroom=-1, tier=standard."""
    snapshot = _make_snapshot(equity=50_000.0, pattern_day_trader=True, daytrade_count=0)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.pdt_qualified is True
    assert result.day_trade_headroom == -1
    assert result.margin_interest_tier.name == "standard"


# ---------------------------------------------------------------------------
# 4. PDT-qualified account at $150k — elite tier
# ---------------------------------------------------------------------------


def test_pdt_qualified_150k_elite_tier() -> None:
    """equity=$150k, pattern_day_trader=True → pdt_qualified=True, headroom=-1, tier=elite."""
    snapshot = _make_snapshot(equity=150_000.0, pattern_day_trader=True, daytrade_count=0)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.pdt_qualified is True
    assert result.day_trade_headroom == -1
    assert result.margin_interest_tier.name == "elite"


# ---------------------------------------------------------------------------
# 5. Sub-PDT with 2 trades
# ---------------------------------------------------------------------------


def test_sub_pdt_2_trades_headroom_1() -> None:
    """equity=$10k, daytrade_count=2, pattern_day_trader=False → headroom=1."""
    snapshot = _make_snapshot(equity=10_000.0, daytrade_count=2, pattern_day_trader=False)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.pdt_qualified is False
    assert result.day_trade_headroom == 1
    assert result.margin_interest_tier.name == "standard"


# ---------------------------------------------------------------------------
# 6. Sub-PDT with 3 trades — headroom clamped to 0
# ---------------------------------------------------------------------------


def test_sub_pdt_3_trades_headroom_0() -> None:
    """equity=$10k, daytrade_count=3, pattern_day_trader=False → headroom=0 (clamped)."""
    snapshot = _make_snapshot(equity=10_000.0, daytrade_count=3, pattern_day_trader=False)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.day_trade_headroom == 0


# ---------------------------------------------------------------------------
# 7. Sub-PDT with 4 trades — headroom clamped (not negative)
# ---------------------------------------------------------------------------


def test_sub_pdt_4_trades_headroom_0_not_negative() -> None:
    """equity=$10k, daytrade_count=4, pattern_day_trader=False → headroom=0, not -1."""
    snapshot = _make_snapshot(equity=10_000.0, daytrade_count=4, pattern_day_trader=False)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.day_trade_headroom == 0
    assert result.day_trade_headroom >= 0


# ---------------------------------------------------------------------------
# 8. Threshold boundary: equity == 25_000.0
# ---------------------------------------------------------------------------


def test_pdt_boundary_inclusive_pdt_true() -> None:
    """equity=25_000.0, pattern_day_trader=True → pdt_qualified=True (inclusive)."""
    snapshot = _make_snapshot(equity=25_000.0, pattern_day_trader=True, daytrade_count=0)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.pdt_qualified is True


def test_pdt_boundary_inclusive_pdt_false() -> None:
    """equity=25_000.0, pattern_day_trader=False → pdt_qualified=False."""
    snapshot = _make_snapshot(equity=25_000.0, pattern_day_trader=False, daytrade_count=0)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.pdt_qualified is False


def test_pdt_boundary_just_below_25k() -> None:
    """equity=24_999.99 → pdt_qualified=False regardless of pattern_day_trader."""
    snapshot = _make_snapshot(equity=24_999.99, pattern_day_trader=True, daytrade_count=0)
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.pdt_qualified is False


# ---------------------------------------------------------------------------
# 9. margin_interest_annual_rate echoes tier.annual_rate
# ---------------------------------------------------------------------------


def test_margin_interest_annual_rate_echoes_tier() -> None:
    """margin_interest_annual_rate must equal margin_interest_tier.annual_rate."""
    for equity, expected_rate in [
        (10_000.0, 0.0625),  # standard tier
        (150_000.0, 0.0475),  # elite tier
    ]:
        snapshot = _make_snapshot(equity=equity, pattern_day_trader=False, daytrade_count=0)
        result = read_venue_account_state(_make_queries(snapshot))

        tier_rate = result.margin_interest_tier.annual_rate
        assert result.margin_interest_annual_rate == pytest.approx(tier_rate)
        assert result.margin_interest_annual_rate == pytest.approx(expected_rate)


# ---------------------------------------------------------------------------
# 10. fetched_at: default UTC, explicit override
# ---------------------------------------------------------------------------


def test_fetched_at_defaults_to_utc_now() -> None:
    """fetched_at defaults to datetime.now(UTC) when not supplied."""
    before = datetime.datetime.now(datetime.UTC)
    snapshot = _make_snapshot()
    result = read_venue_account_state(_make_queries(snapshot))
    after = datetime.datetime.now(datetime.UTC)

    assert result.fetched_at.tzinfo is not None
    assert before <= result.fetched_at <= after


def test_fetched_at_uses_explicit_value() -> None:
    """fetched_at uses the supplied value when caller passes one."""
    explicit = datetime.datetime(2026, 1, 15, 10, 30, 0, tzinfo=datetime.UTC)
    snapshot = _make_snapshot()
    result = read_venue_account_state(_make_queries(snapshot), fetched_at=explicit)

    assert result.fetched_at == explicit


def test_fetched_at_is_tz_aware() -> None:
    """fetched_at must always be tz-aware (has tzinfo)."""
    snapshot = _make_snapshot()
    result = read_venue_account_state(_make_queries(snapshot))

    assert result.fetched_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 11. get_account() called exactly once per invocation
# ---------------------------------------------------------------------------


def test_get_account_called_exactly_once() -> None:
    """read_venue_account_state calls queries.get_account() exactly once."""
    snapshot = _make_snapshot()
    queries = _make_queries(snapshot)

    read_venue_account_state(queries)

    queries.get_account.assert_called_once()


def test_get_account_called_once_per_separate_invocation() -> None:
    """Each call to read_venue_account_state results in exactly one get_account call."""
    snapshot = _make_snapshot()
    queries = _make_queries(snapshot)

    read_venue_account_state(queries)
    read_venue_account_state(queries)

    assert queries.get_account.call_count == 2


# ---------------------------------------------------------------------------
# 12. VenueAccountState is frozen (immutable)
# ---------------------------------------------------------------------------


def test_venue_account_state_is_frozen() -> None:
    """VenueAccountState is a frozen dataclass — fields are immutable."""
    snapshot = _make_snapshot()
    result = read_venue_account_state(_make_queries(snapshot))

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.equity = 99_999.0  # type: ignore[misc]
