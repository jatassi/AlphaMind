"""Tests for the IV-sourcing layer (story 02b; ALP-642 SQL provider).

The library's primary IV source is the data pipeline's IV surface (modelled in
tests by ``IvSurfaceEntry``); when no surface entry covers the requested
strike/expiration, the lookup falls back to the underlying's trailing 30-day
realized volatility (``RealizedVolEntry``). The tests construct fixture data
inline and exercise every branch of the surface→fallback→error chain.

ALP-642 — the production-side ``SqlOptionsIvProvider`` adapter has its own
test class at the bottom of this file; it shares the same surface→fallback→error
contract but resolves surface hits against ``options_contract_snapshots`` rows
keyed by the Polygon-format OCC contract ticker.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory
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
    SqlOptionsIvProvider,
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


# ---------------------------------------------------------------------------
# SqlOptionsIvProvider (ALP-642) — production-side reader against
# options_contract_snapshots, falling back to RealizedVolEntry on miss.
# ---------------------------------------------------------------------------


@pytest.fixture()
def sql_engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def sql_session_factory(sql_engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(sql_engine)


def _add_underlying(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.flush()


def _add_options_contract(session: Session, *, contract_ticker: str, underlying: str) -> None:
    # FK to asset_universe.ticker — add the underlying row first if it is not
    # already in the session.
    existing = session.get(AssetUniverse, f"asset-{underlying.lower()}")
    if existing is None:
        _add_underlying(session, underlying)
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            expiration_date="2026-06-19",
            strike_price=100.0,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
    )
    session.flush()


def _add_options_snapshot(
    session: Session,
    *,
    contract_ticker: str,
    underlying: str,
    snapshot_ts: str,
    implied_volatility: float | None,
) -> None:
    session.add(
        OptionsContractSnapshots(
            snapshot_ts=snapshot_ts,
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            open_interest=200,
            volume_today=100,
            last_price=1.50,
            bid=1.45,
            ask=1.55,
            implied_volatility=implied_volatility,
            delta=0.5,
            gamma=0.05,
            theta=-0.02,
            vega=0.10,
            rho=0.01,
            underlying_price=100.0,
            source="polygon",
            ingested_at=snapshot_ts,
        )
    )


class TestSqlOptionsIvProvider:
    """End-to-end behavioural tests against an in-memory SQLite database."""

    def test_returns_surface_hit_when_snapshot_exists(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """A snapshot row for the requested ``(underlying, strike, expiration,
        contract_type)`` is returned as ``IvSource.SURFACE`` with the stored IV
        verbatim and ``notes=None`` (exact-OCC lookup — no interpolation)."""
        contract = "O:AAPL260619C00100000"
        with sql_session_factory() as session:
            _add_options_contract(session, contract_ticker=contract, underlying="AAPL")
            _add_options_snapshot(
                session,
                contract_ticker=contract,
                underlying="AAPL",
                snapshot_ts="2026-05-15T15:00:00+00:00",
                implied_volatility=0.42,
            )
            session.commit()

        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
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
            implied_volatility=0.42,
            source=IvSource.SURFACE,
            notes=None,
        )

    def test_returns_latest_snapshot_when_multiple_rows_exist(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """When the collector has written multiple snapshots for the same
        contract, the provider returns the row with the largest ``snapshot_ts``
        (a freshly-snapshotted IV beats an older one)."""
        contract = "O:AAPL260619C00100000"
        with sql_session_factory() as session:
            _add_options_contract(session, contract_ticker=contract, underlying="AAPL")
            _add_options_snapshot(
                session,
                contract_ticker=contract,
                underlying="AAPL",
                snapshot_ts="2026-05-10T15:00:00+00:00",
                implied_volatility=0.25,
            )
            _add_options_snapshot(
                session,
                contract_ticker=contract,
                underlying="AAPL",
                snapshot_ts="2026-05-15T15:00:00+00:00",
                implied_volatility=0.42,
            )
            session.commit()

        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol={},
        )

        result = provider.lookup_iv(
            underlying=Symbol("AAPL"),
            strike=100.0,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )

        assert result.implied_volatility == pytest.approx(0.42)
        assert result.source is IvSource.SURFACE

    def test_resolves_put_via_p_in_occ_symbol(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """The OCC symbol the provider builds carries ``P`` for ``ContractType.PUT``;
        a put-side snapshot does not satisfy a call-side request."""
        put_contract = "O:AAPL260619P00100000"
        with sql_session_factory() as session:
            _add_options_contract(session, contract_ticker=put_contract, underlying="AAPL")
            _add_options_snapshot(
                session,
                contract_ticker=put_contract,
                underlying="AAPL",
                snapshot_ts="2026-05-15T15:00:00+00:00",
                implied_volatility=0.55,
            )
            session.commit()

        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol={
                "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.20),
            },
        )

        # Put-side request hits the surface.
        put_result = provider.lookup_iv(
            underlying=Symbol("AAPL"),
            strike=100.0,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.PUT,
            as_of=_AS_OF,
        )
        assert put_result.source is IvSource.SURFACE
        assert put_result.implied_volatility == pytest.approx(0.55)

        # Call-side request for the same strike/expiration misses → fallback.
        call_result = provider.lookup_iv(
            underlying=Symbol("AAPL"),
            strike=100.0,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )
        assert call_result.source is IvSource.REALIZED_VOL_FALLBACK
        assert call_result.implied_volatility == pytest.approx(0.20)

    def test_encodes_fractional_strike_in_thousandths(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """A strike like ``12.50`` becomes ``00012500`` in the OCC symbol —
        avoids binary-float drift that would otherwise miss the snapshot."""
        contract = "O:AAPL260619C00012500"
        with sql_session_factory() as session:
            _add_options_contract(session, contract_ticker=contract, underlying="AAPL")
            _add_options_snapshot(
                session,
                contract_ticker=contract,
                underlying="AAPL",
                snapshot_ts="2026-05-15T15:00:00+00:00",
                implied_volatility=0.60,
            )
            session.commit()

        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol={},
        )

        result = provider.lookup_iv(
            underlying=Symbol("AAPL"),
            strike=12.50,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )
        assert result.source is IvSource.SURFACE
        assert result.implied_volatility == pytest.approx(0.60)

    def test_falls_back_to_realized_vol_when_no_snapshot_row(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """Absent a snapshot row for the contract, the provider returns the
        per-underlying realized-vol scalar with the fallback note."""
        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol={
                "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.27),
            },
        )

        result = provider.lookup_iv(
            underlying=Symbol("AAPL"),
            strike=100.0,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )

        assert result.source is IvSource.REALIZED_VOL_FALLBACK
        assert result.implied_volatility == pytest.approx(0.27)
        assert result.notes is not None
        assert result.notes.startswith("realized_vol_fallback")

    def test_falls_back_when_snapshot_row_exists_but_iv_is_null(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """A snapshot row whose ``implied_volatility`` is NULL is treated as a
        miss — the IV filter on the query excludes it, falling through to the
        realized-vol path. Mirrors the existing fetch_iv_from_options_chains
        reader's contract."""
        contract = "O:AAPL260619C00100000"
        with sql_session_factory() as session:
            _add_options_contract(session, contract_ticker=contract, underlying="AAPL")
            _add_options_snapshot(
                session,
                contract_ticker=contract,
                underlying="AAPL",
                snapshot_ts="2026-05-15T15:00:00+00:00",
                implied_volatility=None,
            )
            session.commit()

        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol={
                "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.27),
            },
        )

        result = provider.lookup_iv(
            underlying=Symbol("AAPL"),
            strike=100.0,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )
        assert result.source is IvSource.REALIZED_VOL_FALLBACK
        assert result.implied_volatility == pytest.approx(0.27)

    def test_raises_when_neither_snapshot_nor_realized_vol_available(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """When the surface misses AND the realized-vol map has no entry, the
        provider raises ``IvLookupError`` naming the request — same terminal
        behaviour as ``FixtureIvProvider``."""
        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol={},
        )

        with pytest.raises(IvLookupError, match="AAPL"):
            provider.lookup_iv(
                underlying=Symbol("AAPL"),
                strike=100.0,
                expiration=date(2026, 6, 19),
                contract_type=ContractType.CALL,
                as_of=_AS_OF,
            )

    def test_realized_vol_mapping_is_consulted_by_reference(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """The continuous-monitor daemon refreshes ``realized_vol`` in place
        every 24h (see continuous_monitor/__main__.py); the provider must
        observe later mutations, not freeze the construction-time snapshot."""
        shared: dict[str, RealizedVolEntry] = {}
        provider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol=shared,
        )

        # Initially no fallback → IvLookupError.
        with pytest.raises(IvLookupError):
            provider.lookup_iv(
                underlying=Symbol("AAPL"),
                strike=100.0,
                expiration=date(2026, 6, 19),
                contract_type=ContractType.CALL,
                as_of=_AS_OF,
            )

        # Mutate in place — refresh adds an entry mid-daemon-lifetime.
        shared["AAPL"] = RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.31)

        result = provider.lookup_iv(
            underlying=Symbol("AAPL"),
            strike=100.0,
            expiration=date(2026, 6, 19),
            contract_type=ContractType.CALL,
            as_of=_AS_OF,
        )
        assert result.source is IvSource.REALIZED_VOL_FALLBACK
        assert result.implied_volatility == pytest.approx(0.31)

    def test_satisfies_iv_provider_protocol(
        self, sql_session_factory: sessionmaker[Session]
    ) -> None:
        """``SqlOptionsIvProvider`` is structurally compatible with ``IvProvider``."""
        provider: IvProvider = SqlOptionsIvProvider(
            sync_session_factory=sql_session_factory,
            realized_vol={},
        )
        assert provider is not None
