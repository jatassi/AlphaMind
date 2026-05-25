"""IV sourcing with realized-vol fallback (story 02b; ALP-642 SQL provider).

The library's Black-Scholes path consults this module for an implied-volatility
estimate at a proposal's ``(strike, expiration, contract_type)``. The primary
source is the data pipeline's IV surface (production-side this is
``options_contract_snapshots`` populated by the Polygon options collector); the
fallback is the underlying's trailing 30-day realized volatility, supplied as a
per-underlying scalar from the data pipeline. The ``IvProvider`` Protocol (in
``types``) expresses the contract; ``FixtureIvProvider`` is the
test-and-bootstrap implementation backed by inline data, and
``SqlOptionsIvProvider`` (ALP-642) is the production-side adapter that resolves
surface hits by exact OCC contract symbol against
``options_contract_snapshots``.

Both implementations conform to the same Protocol so the library's tests assert
behaviour that holds in production.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from alphamind.persistence.models import OptionsContractSnapshots
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    ContractType,
    IvLookupResult,
    IvSource,
)

# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SqlOptionsIvProvider — production adapter (ALP-642)
# ---------------------------------------------------------------------------


def _polygon_options_contract_ticker(
    *,
    underlying: str,
    strike: float,
    expiration: date,
    contract_type: ContractType,
) -> str:
    """Build the Polygon-format OCC contract ticker the collector writes.

    Mirrors ``alphamind.portfolio_state.records.positions.occ_symbol_for_options``
    but takes primitive fields so the guardrail library does not import the
    position-records module. The format is
    ``O:{UNDERLYING}{YYMMDD}{C|P}{strike_milli:08d}`` — the strike is
    multiplied by 1000 and zero-padded to eight digits, rounded to avoid
    binary-float drift on values like ``12.50`` (12500.000000001 → 12500).

    Precondition: ``underlying`` must already be the canonical uppercase
    ticker string the collector writes (e.g., ``"AAPL"``). The function
    does not normalize — a lower/mixed-case input produces an OCC string
    that will not match any Polygon-written row and silently falls
    through to the realized-vol path. This matches the sibling
    ``occ_symbol_for_options`` convention (caller normalizes); every
    callsite the guardrail library exercises today (``ProposedDelta``,
    ``ExistingPosition``) is already uppercase-by-construction.
    """
    expiry = expiration.strftime("%y%m%d")
    cp = "C" if contract_type is ContractType.CALL else "P"
    strike_milli = round(strike * 1000)
    return f"O:{underlying}{expiry}{cp}{strike_milli:08d}"


class SqlOptionsIvProvider:
    """Production ``IvProvider`` backed by ``options_contract_snapshots``.

    Resolves ``(underlying, strike, expiration, contract_type)`` to the
    latest non-NULL ``implied_volatility`` row in the snapshots table,
    keyed by the Polygon-format OCC contract ticker. On surface miss —
    no snapshot row, or row exists but IV is NULL — falls back to the
    per-underlying realized-vol scalar; raises ``IvLookupError`` only
    when both surface and fallback are empty.

    Sync-session-per-lookup so the synchronous ``IvProvider.lookup_iv``
    Protocol holds in async contexts. The brief block on the asyncio loop
    is well within the breach-loop and Phase-1 budgets at current table
    cardinality — the per-call query is ``WHERE contract_ticker = ?
    ORDER BY snapshot_ts DESC LIMIT 1`` against the PK
    ``(snapshot_ts, contract_ticker)``; SQLite scans the leading prefix
    of the PK index, which is acceptable today but is the natural seam
    if a dedicated ``contract_ticker`` index is later added. The
    pre-existing batched async reader in
    ``execution/continuous_monitor/greeks_refresh/iv_provider.py``
    is the per-tick optimization seam if N+1-style lookups become hot.
    No interpolation across strikes/expirations — the production surface
    is dense enough at common strikes that exact-match coverage beats
    interpolation noise for the bulk of proposals; the realized-vol
    fallback covers gaps with the same scalar the legacy fixture path
    used.

    ``as_of`` is intentionally discarded: the provider returns whatever
    the latest snapshot is, irrespective of how stale it has become or
    whether it postdates ``as_of``. This matches the sibling
    ``fetch_iv_from_options_chains`` reader's contract and is correct for
    live runs where ``as_of ~= now`` and the collector cron is healthy.
    Two known limitations the surface does not detect: (a) a stalled
    collector returns a stale IV labelled ``IvSource.SURFACE`` rather
    than falling back, and (b) a replay invocation at a historical
    ``as_of`` sees snapshots newer than that ``as_of``. A future
    enhancement could filter ``snapshot_ts <= as_of`` and/or apply a
    freshness ceiling; the current scope (ALP-642) preserves the
    existing reader's "latest snapshot wins" semantics.

    The ``realized_vol`` mapping is held by reference so the daemon's
    24h in-place refresh in
    ``execution/continuous_monitor/__main__.refresh_realized_vol_map_in_place``
    propagates to the provider without re-construction.
    """

    def __init__(
        self,
        *,
        sync_session_factory: sessionmaker[Session],
        realized_vol: Mapping[str, RealizedVolEntry],
    ) -> None:
        self._sync_session_factory = sync_session_factory
        self._realized_vol = realized_vol

    @property
    def realized_vol(self) -> Mapping[str, RealizedVolEntry]:
        """Public read-only view of the realized-vol fallback mapping.

        Exposed for the subprocess-isolation transport (ALP-650): the
        parent-side ``_SqlIvProviderShim`` extracts this mapping so the
        worker can reconstruct the provider against its own session.
        """
        return self._realized_vol

    def lookup_iv(
        self,
        *,
        underlying: str,
        strike: float,
        expiration: date,
        contract_type: ContractType,
        as_of: datetime,
    ) -> IvLookupResult:
        del as_of  # production surface returns the latest snapshot regardless
        fallback = _RealizedVolFallback(
            store=self._realized_vol,
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            contract_type=contract_type,
        )
        contract_ticker = _polygon_options_contract_ticker(
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            contract_type=contract_type,
        )
        iv = self._read_latest_iv(contract_ticker)
        if iv is None:
            return fallback("realized_vol_fallback_no_snapshot")
        return IvLookupResult(
            implied_volatility=iv,
            source=IvSource.SURFACE,
            notes=None,
        )

    def _read_latest_iv(self, contract_ticker: str) -> float | None:
        """Return the IV from the latest non-NULL snapshot row, or ``None``.

        Matches the existing
        ``execution.continuous_monitor.greeks_refresh.iv_provider.fetch_iv_from_options_chains``
        contract: filter to non-NULL ``implied_volatility``, order by
        ``snapshot_ts DESC``, take one.
        """
        stmt = (
            select(OptionsContractSnapshots.implied_volatility)
            .where(
                OptionsContractSnapshots.contract_ticker == contract_ticker,
                OptionsContractSnapshots.implied_volatility.is_not(None),
            )
            .order_by(OptionsContractSnapshots.snapshot_ts.desc())
            .limit(1)
        )
        with self._sync_session_factory() as session:
            value = session.execute(stmt).scalar_one_or_none()
        if value is None:
            return None
        return float(value)
