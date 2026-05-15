"""Tests for the IV-sourcing layer (story 02b).

The library's primary IV source is the data pipeline's IV surface (modelled in
tests by ``IvSurfaceEntry``); when no surface entry covers the requested
strike/expiration, the lookup falls back to the underlying's trailing 30-day
realized volatility (``RealizedVolEntry``). The tests construct fixture data
inline and exercise every branch of the surface→fallback→error chain.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    FixtureIvProvider,
    IvLookupError,
    IvLookupResult,
    IvProvider,
    IvQuote,
    IvSource,
    IvSurfaceEntry,
    RealizedVolEntry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)


def _quote(
    *,
    strike: float,
    expiration: date,
    contract_type: ContractType = ContractType.CALL,
    iv: float,
) -> IvQuote:
    return IvQuote(
        strike=strike,
        expiration=expiration,
        contract_type=contract_type,
        implied_volatility=iv,
    )


def _surface(underlying: str, *quotes: IvQuote) -> IvSurfaceEntry:
    return IvSurfaceEntry(underlying=underlying, quotes=quotes)


# ---------------------------------------------------------------------------
# Surface-hit paths
# ---------------------------------------------------------------------------


def test_exact_strike_exact_expiration_returns_surface_hit() -> None:
    """An exact ``(strike, expiration, contract_type)`` match returns
    ``IvSource.SURFACE`` with ``notes=None`` and the stored IV verbatim."""
    surface = _surface(
        "AAPL",
        _quote(strike=100.0, expiration=date(2026, 6, 19), iv=0.30),
    )
    provider = FixtureIvProvider(
        surface={"AAPL": surface},
        realized_vol={},
    )

    result = provider.lookup_iv(
        underlying=Symbol("AAPL"),
        strike=100.0,
        expiration=date(2026, 6, 19),
        contract_type=ContractType.CALL,
        as_of=_AS_OF,
    )

    assert result == IvLookupResult(
        implied_volatility=0.30,
        source=IvSource.SURFACE,
        notes=None,
    )


def test_strike_interpolated_linearly_between_listed_strikes() -> None:
    """A strike bracketed by two listed strikes returns linearly-interpolated
    IV with ``notes='strike_interpolated'``; surface source unchanged."""
    expiration = date(2026, 6, 19)
    surface = _surface(
        "AAPL",
        _quote(strike=100.0, expiration=expiration, iv=0.30),
        _quote(strike=110.0, expiration=expiration, iv=0.40),
    )
    provider = FixtureIvProvider(
        surface={"AAPL": surface},
        realized_vol={},
    )

    result = provider.lookup_iv(
        underlying=Symbol("AAPL"),
        strike=105.0,
        expiration=expiration,
        contract_type=ContractType.CALL,
        as_of=_AS_OF,
    )

    assert result.source is IvSource.SURFACE
    assert result.notes == "strike_interpolated"
    assert result.implied_volatility == pytest.approx(0.35)


def test_strike_outside_chain_falls_back_to_realized_vol() -> None:
    """A strike outside the chain bounds falls back to realized vol with
    ``notes='realized_vol_fallback_strike_outside_chain'``. Surface skew
    extrapolation is unreliable; the conservative buffer covers the gap."""
    expiration = date(2026, 6, 19)
    surface = _surface(
        "AAPL",
        _quote(strike=100.0, expiration=expiration, iv=0.30),
        _quote(strike=110.0, expiration=expiration, iv=0.40),
    )
    provider = FixtureIvProvider(
        surface={"AAPL": surface},
        realized_vol={
            "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.25),
        },
    )

    result = provider.lookup_iv(
        underlying=Symbol("AAPL"),
        strike=80.0,
        expiration=expiration,
        contract_type=ContractType.CALL,
        as_of=_AS_OF,
    )

    assert result == IvLookupResult(
        implied_volatility=0.25,
        source=IvSource.REALIZED_VOL_FALLBACK,
        notes="realized_vol_fallback_strike_outside_chain",
    )


def test_expiration_interpolated_linearly_in_calendar_days() -> None:
    """An expiration bracketed by two listed expirations interpolates IV
    linearly in calendar days at the requested strike, after strike
    interpolation against each bracketing expiration."""
    near = date(2026, 5, 15)  # 30d from as_of (2026-04-15 reference offset)
    far = date(2026, 6, 15)
    target = date(2026, 5, 30)  # midpoint between near and far
    surface = _surface(
        "AAPL",
        _quote(strike=100.0, expiration=near, iv=0.28),
        _quote(strike=100.0, expiration=far, iv=0.32),
    )
    provider = FixtureIvProvider(
        surface={"AAPL": surface},
        realized_vol={},
    )

    result = provider.lookup_iv(
        underlying=Symbol("AAPL"),
        strike=100.0,
        expiration=target,
        contract_type=ContractType.CALL,
        as_of=_AS_OF,
    )

    # 15 days from near, 31 days span: 0.28 + (15/31) * (0.32 - 0.28).
    expected_iv = 0.28 + (15 / 31) * (0.32 - 0.28)
    assert result.source is IvSource.SURFACE
    assert result.notes == "expiration_interpolated"
    assert result.implied_volatility == pytest.approx(expected_iv)


def test_expiration_beyond_longest_dated_falls_back_to_realized_vol() -> None:
    """An expiration past the surface's longest-dated quote falls back to
    realized vol with ``notes='realized_vol_fallback_expiration_extrapolated'``."""
    surface = _surface(
        "AAPL",
        _quote(strike=100.0, expiration=date(2026, 5, 15), iv=0.28),
        _quote(strike=100.0, expiration=date(2026, 6, 15), iv=0.32),
    )
    provider = FixtureIvProvider(
        surface={"AAPL": surface},
        realized_vol={
            "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.30),
        },
    )

    result = provider.lookup_iv(
        underlying=Symbol("AAPL"),
        strike=100.0,
        expiration=date(2027, 1, 15),
        contract_type=ContractType.CALL,
        as_of=_AS_OF,
    )

    assert result == IvLookupResult(
        implied_volatility=0.30,
        source=IvSource.REALIZED_VOL_FALLBACK,
        notes="realized_vol_fallback_expiration_extrapolated",
    )


def test_no_chain_with_realized_vol_returns_fallback() -> None:
    """An underlying with no surface entry but a realized-vol entry returns
    the fallback with ``notes='realized_vol_fallback_no_chain'`` and
    ``IvSource.REALIZED_VOL_FALLBACK``."""
    provider = FixtureIvProvider(
        surface={},
        realized_vol={
            "TSLA": RealizedVolEntry(underlying=Symbol("TSLA"), trailing_30d_realized_vol=0.45),
        },
    )

    result = provider.lookup_iv(
        underlying=Symbol("TSLA"),
        strike=200.0,
        expiration=date(2026, 6, 19),
        contract_type=ContractType.CALL,
        as_of=_AS_OF,
    )

    assert result == IvLookupResult(
        implied_volatility=0.45,
        source=IvSource.REALIZED_VOL_FALLBACK,
        notes="realized_vol_fallback_no_chain",
    )


def test_no_chain_no_realized_vol_raises() -> None:
    """An underlying with neither surface nor realized-vol entry raises
    ``IvLookupError`` with a message that names the request."""
    provider = FixtureIvProvider(surface={}, realized_vol={})

    with pytest.raises(IvLookupError, match="TSLA"):
        provider.lookup_iv(
            underlying=Symbol("TSLA"),
            strike=200.0,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )


def test_contract_type_mismatch_falls_back_to_realized_vol() -> None:
    """A surface that contains only puts for the underlying falls back to
    realized vol when a call is requested (and vice versa)."""
    expiration = date(2026, 6, 19)
    surface = _surface(
        "AAPL",
        _quote(
            strike=100.0,
            expiration=expiration,
            contract_type=ContractType.PUT,
            iv=0.30,
        ),
    )
    provider = FixtureIvProvider(
        surface={"AAPL": surface},
        realized_vol={
            "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.25),
        },
    )

    result = provider.lookup_iv(
        underlying=Symbol("AAPL"),
        strike=100.0,
        expiration=expiration,
        contract_type=ContractType.CALL,
        as_of=_AS_OF,
    )

    assert result.source is IvSource.REALIZED_VOL_FALLBACK
    assert result.notes is not None
    assert result.notes.startswith("realized_vol_fallback")
    assert result.implied_volatility == pytest.approx(0.25)


def test_every_successful_lookup_returns_positive_iv() -> None:
    """Every successful path (clean hit, strike-interpolated,
    expiration-interpolated, realized-vol fallback) returns
    ``implied_volatility > 0`` so the ``bs_greeks`` ``_d1``/``_d2`` contract
    (story 02a) is preserved."""
    near = date(2026, 5, 15)
    far = date(2026, 6, 15)
    surface = _surface(
        "AAPL",
        _quote(strike=100.0, expiration=near, iv=0.28),
        _quote(strike=110.0, expiration=near, iv=0.40),
        _quote(strike=100.0, expiration=far, iv=0.32),
    )
    provider = FixtureIvProvider(
        surface={"AAPL": surface},
        realized_vol={
            "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.25),
            "TSLA": RealizedVolEntry(underlying=Symbol("TSLA"), trailing_30d_realized_vol=0.45),
        },
    )

    requests = [
        # Clean surface hit.
        ("AAPL", 100.0, near),
        # Strike interpolation.
        ("AAPL", 105.0, near),
        # Strike outside chain → realized vol fallback.
        ("AAPL", 80.0, near),
        # Expiration interpolation between near and far.
        ("AAPL", 100.0, date(2026, 5, 30)),
        # Expiration extrapolated → realized vol fallback.
        ("AAPL", 100.0, date(2027, 1, 15)),
        # No-chain underlying → realized vol fallback.
        ("TSLA", 200.0, date(2026, 6, 19)),
    ]

    for underlying, strike, expiration in requests:
        result = provider.lookup_iv(
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )
        assert result.implied_volatility > 0, (
            f"non-positive IV for {underlying} {strike}/{expiration}"
        )


def test_fixture_iv_provider_satisfies_protocol() -> None:
    """``FixtureIvProvider`` is structurally compatible with ``IvProvider``."""
    provider: IvProvider = FixtureIvProvider(surface={}, realized_vol={})
    assert provider is not None
