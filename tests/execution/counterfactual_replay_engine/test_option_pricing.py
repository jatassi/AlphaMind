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

from alphamind._kernel.money import price
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    price_option_at_underlying_bar,
)
from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_price
from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType

_BAR_TS = datetime(2026, 1, 10, 14, 0, tzinfo=UTC)
_EXPIRATION = date(2026, 2, 20)
_RFR = 0.045
_IV = 0.30


def _expected_tte() -> float:
    expiration_dt_utc = datetime(2026, 2, 20, 0, 0, tzinfo=UTC)
    return (expiration_dt_utc - _BAR_TS).total_seconds() / (365.25 * 86400)


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
    assert result == price(Decimal(str(expected)))


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
    assert result == price(Decimal(str(expected)))


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
