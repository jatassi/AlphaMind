"""Tests for the BS pricing wrapper (ALP-562 §1).

``price_option_at_underlying_bar`` wraps the guardrail-evaluation
:func:`bs_price` primitive: it computes ``time_to_expiration_years`` from the
calendar gap between the bar timestamp and the expiration, then returns the
premium as a :class:`Price`. The wrapper is exercised against ``bs_price``'s
own output (a real call, not a mock) so the test pins the composition, not a
hard-coded premium.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from alphamind._kernel.money import money
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    price_option_at_underlying_bar,
)
from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_price
from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType

_BAR_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_EXPIRATION = date(2026, 2, 20)
_RFR = 0.045
_IV = 0.30


def _expiration_instant_utc() -> datetime:
    # US equity options expire at 16:00 America/New_York; February is EST (UTC-5).
    return datetime(2026, 2, 20, 16, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def _expected_tte() -> float:
    return (_expiration_instant_utc() - _BAR_TS).total_seconds() / (365.25 * 86400)


def test_call_premium_matches_bs_price() -> None:
    result = price_option_at_underlying_bar(
        underlying_open=150.0,
        strike=Decimal("155.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=_BAR_TS,
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )

    expected = bs_price(
        spot=150.0,
        strike=155.0,
        time_to_expiration_years=_expected_tte(),
        risk_free_rate=_RFR,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )
    assert result == money(Decimal(str(expected)))


def test_put_premium_matches_bs_price() -> None:
    result = price_option_at_underlying_bar(
        underlying_open=150.0,
        strike=Decimal("145.00"),
        expiration=_EXPIRATION,
        contract_type="put",
        bar_timestamp=_BAR_TS,
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )

    expected = bs_price(
        spot=150.0,
        strike=145.0,
        time_to_expiration_years=_expected_tte(),
        risk_free_rate=_RFR,
        implied_volatility=_IV,
        contract_type=ContractType.PUT,
    )
    assert result == money(Decimal(str(expected)))


def test_time_to_expiration_is_calendar_day_anchored() -> None:
    # A bar one day closer to expiry than another yields a shorter TTE and (for
    # an OTM call) a lower premium — confirms TTE is the calendar gap, not a
    # fixed constant.
    far = price_option_at_underlying_bar(
        underlying_open=150.0,
        strike=Decimal("160.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=_BAR_TS,
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )
    near = price_option_at_underlying_bar(
        underlying_open=150.0,
        strike=Decimal("160.00"),
        expiration=_EXPIRATION,
        contract_type="call",
        bar_timestamp=datetime(2026, 2, 10, 14, 0, tzinfo=UTC),
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )
    assert near < far


def test_expiry_day_morning_bar_has_positive_time_value() -> None:
    # An expiry-day 09:30 ET bar (= 14:30 UTC) is ~6.5h before the 16:00 ET
    # close. Anchoring expiry at midnight UTC would put it ~9.5h *after* the bar
    # in clock terms but yields a negative TTE (midnight UTC precedes the bar),
    # crashing or mispricing every expiry-day replay. With the 16:00 ET anchor
    # the TTE is positive and an ATM option still carries time value.
    expiry = date(2026, 2, 20)
    morning_bar = datetime(2026, 2, 20, 14, 30, tzinfo=UTC)  # 09:30 ET (EST)
    expected_tte = (_expiration_instant_utc() - morning_bar).total_seconds() / (365.25 * 86400)
    assert expected_tte > 0

    result = price_option_at_underlying_bar(
        underlying_open=150.0,
        strike=Decimal("150.00"),
        expiration=expiry,
        contract_type="call",
        bar_timestamp=morning_bar,
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )
    expected = bs_price(
        spot=150.0,
        strike=150.0,
        time_to_expiration_years=expected_tte,
        risk_free_rate=_RFR,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )
    assert result == money(Decimal(str(expected)))
    assert result > money("0")  # positive TTE → positive ATM premium


def test_otm_at_expiry_prices_to_zero_without_crashing() -> None:
    # A deep-OTM call on an after-the-close expiry bar has tte <= 0, so bs_price
    # returns the 0.0 intrinsic value. The wrapper must return a zero Money
    # premium, not crash on the strictly-positive price() constructor.
    after_close = datetime(2026, 2, 20, 22, 0, tzinfo=UTC)  # 17:00 ET, past expiry
    result = price_option_at_underlying_bar(
        underlying_open=100.0,
        strike=Decimal("150.00"),
        expiration=date(2026, 2, 20),
        contract_type="call",
        bar_timestamp=after_close,
        implied_volatility=_IV,
        risk_free_rate=_RFR,
    )
    assert result == money("0")
