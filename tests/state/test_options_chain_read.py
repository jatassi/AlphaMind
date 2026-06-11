"""Tests for the options chain-slice read + selection core (ALP-948).

``select_chain_slice`` is exercised as a pure function on plain
:class:`ContractQuote` tuples — no DB session. :class:`SqlOptionsChainReader`
is exercised against a seeded on-disk SQLite database, mirroring the
``test_sql_option_price_provider`` conventions (on-disk because the reader's
sync sessionmaker is a distinct ``Engine`` from the seeding engine).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

# Prime ``state.invocation_context`` before ``state.repository.__init__``
# (see test_sql_option_price_provider.py for the partial-import rationale).
import alphamind.state.invocation_context  # noqa: F401
from alphamind._kernel.ids import OccSymbol
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationTickerBaseline,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.options_chain_read import (
    ChainFilterParams,
    ContractQuote,
    SqlOptionsChainReader,
    build_options_context,
    occ_symbol_for_contract,
    select_chain_slice,
)

_NOW = datetime(2026, 6, 10, 14, 30, 0, tzinfo=UTC)

_PARAMS = ChainFilterParams(
    strike_band_pct=15.0,
    min_days_to_expiration=5,
    max_days_to_expiration=45,
    min_open_interest=100,
    max_contracts_rendered=24,
)


def _quote(
    *,
    strike: float = 100.0,
    expiration: date = date(2026, 7, 2),  # 22 DTE from _NOW
    contract_type: str = "call",
    open_interest: int = 500,
    bid: float = 4.90,
    ask: float = 5.10,
    underlying: str = "AAPL",
) -> ContractQuote:
    return ContractQuote(
        occ_symbol=occ_symbol_for_contract(
            underlying=underlying,
            expiration=expiration,
            strike=strike,
            contract_type=contract_type,  # type: ignore[arg-type]
        ),
        underlying=underlying,
        expiration=expiration,
        strike=strike,
        contract_type=contract_type,  # type: ignore[arg-type]
        bid=bid,
        ask=ask,
        implied_volatility=0.32,
        delta=0.51,
        open_interest=open_interest,
        snapshot_ts=_NOW - timedelta(minutes=10),
    )


# ---------------------------------------------------------------------------
# select_chain_slice — pure selection policy
# ---------------------------------------------------------------------------


def test_select_chain_slice_drops_strikes_outside_band_and_names_them() -> None:
    inside = _quote(strike=110.0)
    outside = _quote(strike=120.0)  # ±15% of spot 100 → band is 85..115

    chain = select_chain_slice(
        (inside, outside), underlying="AAPL", spot=100.0, as_of=_NOW, params=_PARAMS
    )

    assert chain.contracts == (inside,)
    assert chain.omitted is not None
    assert "1 outside the ±15% strike band" in chain.omitted


def test_select_chain_slice_drops_expirations_outside_dte_window() -> None:
    inside = _quote(expiration=date(2026, 7, 2))
    too_near = _quote(expiration=date(2026, 6, 12), strike=101.0)  # 2 DTE < 5
    too_far = _quote(expiration=date(2026, 9, 18), strike=102.0)  # 100 DTE > 45

    chain = select_chain_slice(
        (inside, too_near, too_far), underlying="AAPL", spot=100.0, as_of=_NOW, params=_PARAMS
    )

    assert chain.contracts == (inside,)
    assert chain.omitted is not None
    assert "2 outside the 5-45 DTE window" in chain.omitted


def test_select_chain_slice_drops_contracts_below_open_interest_floor() -> None:
    liquid = _quote(open_interest=100)
    illiquid = _quote(open_interest=99, strike=101.0)

    chain = select_chain_slice(
        (liquid, illiquid), underlying="AAPL", spot=100.0, as_of=_NOW, params=_PARAMS
    )

    assert chain.contracts == (liquid,)
    assert chain.omitted is not None
    assert "1 below the open-interest floor 100" in chain.omitted


def test_select_chain_slice_caps_to_nearest_the_money_and_names_the_drop() -> None:
    params = ChainFilterParams(
        strike_band_pct=15.0,
        min_days_to_expiration=5,
        max_days_to_expiration=45,
        min_open_interest=100,
        max_contracts_rendered=2,
    )
    near = _quote(strike=100.0)
    nearer = _quote(strike=101.0)
    far = _quote(strike=110.0)

    chain = select_chain_slice(
        (far, near, nearer), underlying="AAPL", spot=100.0, as_of=_NOW, params=params
    )

    assert {quote.strike for quote in chain.contracts} == {100.0, 101.0}
    assert chain.omitted is not None
    assert "1 furthest-from-money beyond the 2-contract cap" in chain.omitted


def test_select_chain_slice_omitted_is_none_when_nothing_dropped() -> None:
    rows = (_quote(strike=100.0), _quote(strike=105.0))

    chain = select_chain_slice(rows, underlying="AAPL", spot=100.0, as_of=_NOW, params=_PARAMS)

    assert chain.omitted is None
    assert len(chain.contracts) == 2


def test_select_chain_slice_orders_by_expiration_then_strike() -> None:
    later = _quote(expiration=date(2026, 7, 10), strike=95.0)
    earlier_high = _quote(expiration=date(2026, 7, 2), strike=105.0)
    earlier_low = _quote(expiration=date(2026, 7, 2), strike=95.0)

    chain = select_chain_slice(
        (later, earlier_high, earlier_low),
        underlying="AAPL",
        spot=100.0,
        as_of=_NOW,
        params=_PARAMS,
    )

    assert [(q.expiration, q.strike) for q in chain.contracts] == [
        (date(2026, 7, 2), 95.0),
        (date(2026, 7, 2), 105.0),
        (date(2026, 7, 10), 95.0),
    ]


# ---------------------------------------------------------------------------
# occ_symbol_for_contract
# ---------------------------------------------------------------------------


def test_occ_symbol_for_contract_matches_collector_key_format() -> None:
    occ = occ_symbol_for_contract(
        underlying="NVDA",
        expiration=date(2026, 6, 19),
        strike=800.0,
        contract_type="call",
    )
    assert occ == "NVDA260619C00800000"


def test_occ_symbol_for_contract_drops_share_class_dot_and_rounds_strike() -> None:
    occ = occ_symbol_for_contract(
        underlying="BRK.B",
        expiration=date(2026, 6, 19),
        strike=12.50,
        contract_type="put",
    )
    assert occ == "BRKB260619P00012500"


# ---------------------------------------------------------------------------
# SqlOptionsChainReader — seeded SQLite
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_factory(tmp_path: Path) -> sessionmaker[Session]:
    db_path = tmp_path / "alphamind.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_underlying(session: Session, underlying: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{underlying.lower()}",
            ticker=underlying,
            full_name=f"{underlying} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-01T00:00:00Z",
        )
    )
    session.flush()


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _seed_contract(
    session: Session,
    *,
    underlying: str,
    strike: float,
    expiration: str,
    contract_type: str = "call",
) -> str:
    contract_ticker = "O:" + occ_symbol_for_contract(
        underlying=underlying,
        expiration=date.fromisoformat(expiration),
        strike=strike,
        contract_type=contract_type,  # type: ignore[arg-type]
    )
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            expiration_date=expiration,
            strike_price=strike,
            contract_type=contract_type,
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at=_iso(_NOW),
            source="test",
        )
    )
    session.flush()
    return contract_ticker


def _seed_snapshot(
    session: Session,
    *,
    contract_ticker: str,
    underlying: str,
    snapshot_ts: datetime,
    bid: float | None = 4.90,
    ask: float | None = 5.10,
    implied_volatility: float | None = 0.32,
    delta: float | None = 0.51,
    open_interest: int | None = 500,
    underlying_price: float | None = 100.0,
) -> None:
    session.add(
        OptionsContractSnapshots(
            snapshot_ts=_iso(snapshot_ts),
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            open_interest=open_interest,
            volume_today=50,
            last_price=5.00,
            bid=bid,
            ask=ask,
            implied_volatility=implied_volatility,
            delta=delta,
            gamma=0.05,
            theta=-0.03,
            vega=0.10,
            rho=0.02,
            underlying_price=underlying_price,
            source="test",
            ingested_at=_iso(snapshot_ts),
        )
    )


def _reader(sync_factory: sessionmaker[Session]) -> SqlOptionsChainReader:
    return SqlOptionsChainReader(
        sync_session_factory=sync_factory,
        params=_PARAMS,
        now=lambda: _NOW,
    )


def test_chain_slice_returns_latest_snapshot_per_contract(
    sync_factory: sessionmaker[Session],
) -> None:
    with sync_factory() as session, session.begin():
        _seed_underlying(session, "AAPL")
        ct = _seed_contract(session, underlying="AAPL", strike=100.0, expiration="2026-07-02")
        _seed_snapshot(
            session,
            contract_ticker=ct,
            underlying="AAPL",
            snapshot_ts=_NOW - timedelta(hours=2),
            bid=3.00,
            ask=3.20,
            underlying_price=98.0,
        )
        _seed_snapshot(
            session,
            contract_ticker=ct,
            underlying="AAPL",
            snapshot_ts=_NOW - timedelta(minutes=10),
            bid=4.90,
            ask=5.10,
            underlying_price=100.0,
        )

    chain = _reader(sync_factory).chain_slice("AAPL")

    assert chain is not None
    assert len(chain.contracts) == 1
    quote = chain.contracts[0]
    assert quote.bid == pytest.approx(4.90)
    assert quote.ask == pytest.approx(5.10)
    assert quote.snapshot_ts == _NOW - timedelta(minutes=10)
    # Spot rides the freshest snapshot's underlying_price.
    assert chain.spot == pytest.approx(100.0)


def test_chain_slice_returns_none_for_underlying_without_data(
    sync_factory: sessionmaker[Session],
) -> None:
    assert _reader(sync_factory).chain_slice("TSLA") is None


def test_chain_slice_notes_contracts_without_usable_quotes(
    sync_factory: sessionmaker[Session],
) -> None:
    with sync_factory() as session, session.begin():
        _seed_underlying(session, "AAPL")
        quoted = _seed_contract(session, underlying="AAPL", strike=100.0, expiration="2026-07-02")
        unquoted = _seed_contract(session, underlying="AAPL", strike=105.0, expiration="2026-07-02")
        _seed_snapshot(session, contract_ticker=quoted, underlying="AAPL", snapshot_ts=_NOW)
        _seed_snapshot(
            session,
            contract_ticker=unquoted,
            underlying="AAPL",
            snapshot_ts=_NOW,
            bid=None,
            ask=None,
        )

    chain = _reader(sync_factory).chain_slice("AAPL")

    assert chain is not None
    assert len(chain.contracts) == 1
    assert chain.omitted is not None
    assert "1 contract(s) without a usable two-sided quote" in chain.omitted


def test_latest_quote_resolves_compressed_occ_to_polygon_key(
    sync_factory: sessionmaker[Session],
) -> None:
    with sync_factory() as session, session.begin():
        _seed_underlying(session, "AAPL")
        ct = _seed_contract(session, underlying="AAPL", strike=100.0, expiration="2026-07-02")
        _seed_snapshot(session, contract_ticker=ct, underlying="AAPL", snapshot_ts=_NOW)

    occ = occ_symbol_for_contract(
        underlying="AAPL", expiration=date(2026, 7, 2), strike=100.0, contract_type="call"
    )
    quote = _reader(sync_factory).latest_quote(occ)

    assert quote is not None
    assert quote.strike == pytest.approx(100.0)
    assert quote.nbbo_mid == pytest.approx(5.00)


def test_latest_quote_returns_none_for_unknown_contract(
    sync_factory: sessionmaker[Session],
) -> None:
    occ = OccSymbol("AAPL260702C00100000")
    assert _reader(sync_factory).latest_quote(occ) is None


def test_options_context_carries_surviving_expirations_and_writes_no_baseline(
    sync_factory: sessionmaker[Session],
) -> None:
    with sync_factory() as session, session.begin():
        _seed_underlying(session, "AAPL")
        near = _seed_contract(session, underlying="AAPL", strike=100.0, expiration="2026-07-02")
        far = _seed_contract(
            session, underlying="AAPL", strike=100.0, expiration="2026-09-18"
        )  # 100 DTE — dropped by the expiration window
        _seed_snapshot(session, contract_ticker=near, underlying="AAPL", snapshot_ts=_NOW)
        _seed_snapshot(session, contract_ticker=far, underlying="AAPL", snapshot_ts=_NOW)

    context = _reader(sync_factory).options_context("AAPL")

    assert context is not None
    assert context.liquid_expirations == (date(2026, 7, 2),)
    # A bootstrap-thin IV history yields no calibrated rank.
    assert context.iv_rank is None
    # Read-only reuse: assembling context must not write baseline rows.
    with sync_factory() as session:
        baseline_rows = session.execute(select(DistillationTickerBaseline)).all()
    assert baseline_rows == []


def test_options_context_returns_none_without_chain_data(
    sync_factory: sessionmaker[Session],
) -> None:
    assert _reader(sync_factory).options_context("TSLA") is None


def test_build_options_context_includes_only_tickers_with_chains(
    sync_factory: sessionmaker[Session],
) -> None:
    with sync_factory() as session, session.begin():
        _seed_underlying(session, "AAPL")
        ct = _seed_contract(session, underlying="AAPL", strike=100.0, expiration="2026-07-02")
        _seed_snapshot(session, contract_ticker=ct, underlying="AAPL", snapshot_ts=_NOW)

    context = build_options_context(_reader(sync_factory), tickers=("AAPL", "TSLA"))

    assert set(context) == {"AAPL"}


def test_latest_quote_skips_newer_unusable_snapshot(
    sync_factory: sessionmaker[Session],
) -> None:
    """A newer garbage row (crossed quote) must not mask the older usable one
    the chain tool anchored against — latest_quote returns the latest USABLE
    snapshot."""
    with sync_factory() as session, session.begin():
        _seed_underlying(session, "AAPL")
        ct = _seed_contract(session, underlying="AAPL", strike=100.0, expiration="2026-07-02")
        _seed_snapshot(
            session,
            contract_ticker=ct,
            underlying="AAPL",
            snapshot_ts=_NOW - timedelta(minutes=30),
            bid=4.90,
            ask=5.10,
        )
        _seed_snapshot(
            session,
            contract_ticker=ct,
            underlying="AAPL",
            snapshot_ts=_NOW - timedelta(minutes=5),
            bid=5.30,
            ask=5.10,  # crossed — unusable
        )

    occ = occ_symbol_for_contract(
        underlying="AAPL", expiration=date(2026, 7, 2), strike=100.0, contract_type="call"
    )
    quote = _reader(sync_factory).latest_quote(occ)

    assert quote is not None
    assert quote.nbbo_mid == pytest.approx(5.00)
    assert quote.snapshot_ts == _NOW - timedelta(minutes=30)


def test_chain_filter_params_from_config_projects_the_five_filter_fields() -> None:
    from alphamind.config.models.options_chain import OptionsChainConfig

    config = OptionsChainConfig(
        strike_band_pct=15.0,
        min_days_to_expiration=5,
        max_days_to_expiration=45,
        min_open_interest=100,
        max_contracts_rendered=24,
        premium_staleness_tolerance_pct=10.0,
    )

    assert ChainFilterParams.from_config(config) == _PARAMS
