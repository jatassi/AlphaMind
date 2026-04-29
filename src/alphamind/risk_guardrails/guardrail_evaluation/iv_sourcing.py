"""IV sourcing with realized-vol fallback (story 02b).

The library's Black-Scholes path consults this module for an implied-volatility
estimate at a proposal's ``(strike, expiration, contract_type)``. The primary
source is the data pipeline's IV surface (production-side this is
``options_contract_snapshots`` populated by the Polygon options collector); the
fallback is the underlying's trailing 30-day realized volatility, supplied as a
per-underlying scalar from the data pipeline. The ``IvProvider`` Protocol
expresses the contract; ``FixtureIvProvider`` is the test-and-bootstrap
implementation backed by inline data.

The Polygon-backed production adapter lands when the options collector is
built (per ``project-tracker.md`` § Backlog → Forward-trigger entries); both
implementations conform to the same Protocol so the library's tests assert
behaviour that holds in production.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from typing import Protocol

from alphamind.risk_guardrails.guardrail_evaluation.types import (
    ContractType,
    IvSource,
)

# ---------------------------------------------------------------------------
# Protocol surface and result/error types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IvLookupResult:
    """Outcome of a single ``IvProvider.lookup_iv`` call.

    ``notes`` is ``None`` for a clean surface hit; populated with a short tag
    (e.g., ``"strike_interpolated"``, ``"expiration_interpolated"``,
    ``"realized_vol_fallback_no_chain"``,
    ``"realized_vol_fallback_strike_outside_chain"``,
    ``"realized_vol_fallback_expiration_extrapolated"``) whenever the lookup
    interpolated non-trivially or fell back. ``notes`` is informational —
    callers log it for IV-provenance auditing but the projection math does not
    branch on its value.
    """

    implied_volatility: float
    source: IvSource
    notes: str | None


class IvLookupError(Exception):
    """Raised when neither the surface nor the realized-vol fallback can
    produce an IV for the requested ``(underlying, strike, expiration,
    contract_type)``.

    Conditions:
      - The underlying has no options chain in the surface AND no historical
        price series sufficient for a 30-day realized-vol estimate.
      - The contract type is ``CALL``/``PUT`` but the surface contains no rows
        of that type for the underlying within the lookback window AND
        realized-vol fallback is unavailable.
    """


class IvProvider(Protocol):
    """The library's IV-sourcing contract.

    Concrete implementations: ``FixtureIvProvider`` (test/bootstrap) and the
    Polygon-backed production adapter that reads ``options_contract_snapshots``
    when the options collector lands. ``lookup_iv`` is total — every successful
    path returns an ``IvLookupResult`` with positive ``implied_volatility``;
    the inability to produce one raises ``IvLookupError``.
    """

    def lookup_iv(
        self,
        *,
        underlying: str,
        strike: float,
        expiration: date,
        contract_type: ContractType,
        as_of: datetime,
    ) -> IvLookupResult: ...


# ---------------------------------------------------------------------------
# Fixture-backed surface and realized-vol entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IvQuote:
    """One row of an IV surface — a stripped slice of
    ``options_contract_snapshots``: a single ``(strike, expiration,
    contract_type)`` IV observation for an underlying."""

    strike: float
    expiration: date
    contract_type: ContractType
    implied_volatility: float


@dataclass(frozen=True, slots=True)
class IvSurfaceEntry:
    """All surface rows for one underlying, materialised inline for tests."""

    underlying: str
    quotes: tuple[IvQuote, ...]


@dataclass(frozen=True, slots=True)
class RealizedVolEntry:
    """The fallback IV for one underlying — a trailing-30-day realized-vol
    scalar produced by the data pipeline (``std(daily log returns) * sqrt(252)``).
    Computing this value is out of scope for the library; the entry is the
    library's read-only view."""

    underlying: str
    trailing_30d_realized_vol: float


# ---------------------------------------------------------------------------
# FixtureIvProvider — surface→fallback→error chain
# ---------------------------------------------------------------------------


class FixtureIvProvider:
    """In-memory ``IvProvider`` for tests and the bootstrap path.

    The constructor accepts per-underlying surface and realized-vol mappings;
    ``lookup_iv`` walks the documented chain (exact hit → strike interpolation
    → expiration interpolation → realized-vol fallback → error).
    """

    def __init__(
        self,
        *,
        surface: Mapping[str, IvSurfaceEntry],
        realized_vol: Mapping[str, RealizedVolEntry],
    ) -> None:
        self._surface = surface
        self._realized_vol = realized_vol

    def lookup_iv(
        self,
        *,
        underlying: str,
        strike: float,
        expiration: date,
        contract_type: ContractType,
        as_of: datetime,
    ) -> IvLookupResult:
        del as_of  # production adapter consults it; the fixture does not
        fallback = _RealizedVolFallback(
            store=self._realized_vol,
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            contract_type=contract_type,
        )
        entry = self._surface.get(underlying)
        if entry is None:
            return fallback("realized_vol_fallback_no_chain")

        same_type = [q for q in entry.quotes if q.contract_type is contract_type]
        if not same_type:
            return fallback("realized_vol_fallback_no_chain")

        same_expiration = [q for q in same_type if q.expiration == expiration]
        if same_expiration:
            return _iv_at_strike(same_expiration, strike) or fallback(
                "realized_vol_fallback_strike_outside_chain"
            )

        bracket = _bracketing_expirations(sorted({q.expiration for q in same_type}), expiration)
        if bracket is None:
            return fallback("realized_vol_fallback_expiration_extrapolated")
        near_exp, far_exp = bracket
        near_iv = _iv_at_strike([q for q in same_type if q.expiration == near_exp], strike)
        far_iv = _iv_at_strike([q for q in same_type if q.expiration == far_exp], strike)
        if near_iv is None or far_iv is None:
            return fallback("realized_vol_fallback_strike_outside_chain")

        weight = (expiration - near_exp).days / (far_exp - near_exp).days
        return IvLookupResult(
            implied_volatility=near_iv.implied_volatility
            + weight * (far_iv.implied_volatility - near_iv.implied_volatility),
            source=IvSource.SURFACE,
            notes="expiration_interpolated",
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RealizedVolFallback:
    """Captures the lookup request so callers invoke fallback with just notes.

    Centralises the surface→fallback transition: the surface filters reduce a
    request to "no value here, here's why"; this carries the request forward
    to the realized-vol read or the terminal ``IvLookupError``.
    """

    store: Mapping[str, RealizedVolEntry]
    underlying: str
    strike: float
    expiration: date
    contract_type: ContractType

    def __call__(self, notes: str) -> IvLookupResult:
        rv = self.store.get(self.underlying)
        if rv is None:
            raise IvLookupError(
                f"no IV available for {self.underlying} "
                f"{self.strike}/{self.expiration}/{self.contract_type.value}"
            )
        return IvLookupResult(
            implied_volatility=rv.trailing_30d_realized_vol,
            source=IvSource.REALIZED_VOL_FALLBACK,
            notes=notes,
        )


def _iv_at_strike(
    quotes_at_expiration: list[IvQuote],
    strike: float,
) -> IvLookupResult | None:
    """Resolve IV at ``strike`` against quotes filtered to one
    ``(contract_type, expiration)`` slice.

    Returns ``None`` when the strike is outside the chain bounds (the caller
    decides whether that's a fallback or a different branch). Exact match →
    surface hit, ``notes=None``. Strictly bracketed → linear-in-strike
    interpolation, ``notes="strike_interpolated"``.
    """
    for quote in quotes_at_expiration:
        if quote.strike == strike:
            return IvLookupResult(
                implied_volatility=quote.implied_volatility,
                source=IvSource.SURFACE,
                notes=None,
            )

    sorted_quotes = sorted(quotes_at_expiration, key=lambda q: q.strike)
    for low, high in pairwise(sorted_quotes):
        if low.strike < strike < high.strike:
            weight = (strike - low.strike) / (high.strike - low.strike)
            return IvLookupResult(
                implied_volatility=low.implied_volatility
                + weight * (high.implied_volatility - low.implied_volatility),
                source=IvSource.SURFACE,
                notes="strike_interpolated",
            )

    return None


def _bracketing_expirations(
    sorted_expirations: list[date], target: date
) -> tuple[date, date] | None:
    """Return the closest pair ``(low, high)`` with ``low <= target <= high``,
    or ``None`` when ``target`` is outside the surface's date range."""
    for low, high in pairwise(sorted_expirations):
        if low <= target <= high:
            return low, high
    return None


# Bind ``IvProvider`` into ``types`` so ``typing.get_type_hints(MarketInputs)``
# resolves the ``"IvProvider"`` forward annotation against the module where
# ``MarketInputs`` is defined. ``types`` is fully loaded by this point (the
# import at the top of this file brought it in), so writing the attribute is
# the cycle-free way to keep the Protocol's canonical home here while
# satisfying the typing-introspection contract there.
from alphamind.risk_guardrails.guardrail_evaluation import types as _types  # noqa: E402

_types.IvProvider = IvProvider  # type: ignore[attr-defined]
del _types
