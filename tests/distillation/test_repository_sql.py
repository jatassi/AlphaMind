"""Tests for SqlDistillationRepository.load_options_snapshot_pairs_for_underlying.

The batched (today, prior) snapshot loader replaced two per-contract N+1
methods that degraded to a partial scan over the 13.5M-row snapshots table
on production. These tests exercise the batched query end-to-end against an
in-memory SQLite database, covering every shape combination the q3 loader
needs to handle: today-only, prior-only, both, neither, and isolation
between different underlyings.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


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
    # Flush so subsequent contract inserts (FK → asset_universe.ticker) see
    # the parent row regardless of SQLAlchemy's unit-of-work ordering.
    session.flush()


def _add_contract(
    session: Session,
    *,
    contract_ticker: str,
    underlying: str,
    contract_type: str = "call",
) -> None:
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            expiration_date="2026-06-19",
            strike_price=100.0,
            contract_type=contract_type,
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
    )
    # Flush so subsequent snapshot inserts (FK → contract_ticker) see the parent
    # row regardless of SQLAlchemy's unit-of-work ordering heuristics.
    session.flush()


def _add_snapshot(
    session: Session,
    *,
    contract_ticker: str,
    underlying: str,
    snapshot_ts: str,
    volume_today: int | None = 100,
    open_interest: int | None = 200,
) -> None:
    session.add(
        OptionsContractSnapshots(
            snapshot_ts=snapshot_ts,
            contract_ticker=contract_ticker,
            underlying_ticker=underlying,
            open_interest=open_interest,
            volume_today=volume_today,
            last_price=1.50,
            bid=1.45,
            ask=1.55,
            implied_volatility=0.35,
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


class TestLoadOptionsSnapshotPairsForUnderlying:
    def test_returns_today_and_prior_pair_when_both_exist(self, session: Session) -> None:
        _add_underlying(session, "META")
        _add_contract(session, contract_ticker="META240619C00100000", underlying="META")
        _add_snapshot(
            session,
            contract_ticker="META240619C00100000",
            underlying="META",
            snapshot_ts="2026-05-10T00:00:00Z",
            volume_today=50,
            open_interest=500,
        )
        _add_snapshot(
            session,
            contract_ticker="META240619C00100000",
            underlying="META",
            snapshot_ts="2026-05-15T00:00:00Z",
            volume_today=150,
            open_interest=600,
        )
        session.commit()

        repo = SqlDistillationRepository(session)
        pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )

        assert set(pairs.keys()) == {"META240619C00100000"}
        today, prior = pairs["META240619C00100000"]
        assert today is not None
        assert today.snapshot_ts == "2026-05-15T00:00:00Z"
        assert today.volume_today == 150
        assert today.open_interest == 600
        assert prior is not None
        assert prior.snapshot_ts == "2026-05-10T00:00:00Z"
        assert prior.volume_today == 50
        assert prior.open_interest == 500

    def test_returns_today_only_when_no_prior_snapshot(self, session: Session) -> None:
        _add_underlying(session, "META")
        _add_contract(session, contract_ticker="META240619C00200000", underlying="META")
        _add_snapshot(
            session,
            contract_ticker="META240619C00200000",
            underlying="META",
            snapshot_ts="2026-05-15T00:00:00Z",
        )
        session.commit()

        repo = SqlDistillationRepository(session)
        pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )

        assert set(pairs.keys()) == {"META240619C00200000"}
        today, prior = pairs["META240619C00200000"]
        assert today is not None
        assert today.snapshot_ts == "2026-05-15T00:00:00Z"
        assert prior is None

    def test_returns_prior_only_when_no_today_snapshot(self, session: Session) -> None:
        _add_underlying(session, "META")
        _add_contract(session, contract_ticker="META240619P00100000", underlying="META")
        _add_snapshot(
            session,
            contract_ticker="META240619P00100000",
            underlying="META",
            snapshot_ts="2026-05-10T00:00:00Z",
        )
        session.commit()

        repo = SqlDistillationRepository(session)
        pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )

        assert set(pairs.keys()) == {"META240619P00100000"}
        today, prior = pairs["META240619P00100000"]
        assert today is None
        assert prior is not None
        assert prior.snapshot_ts == "2026-05-10T00:00:00Z"

    def test_contract_with_no_snapshots_is_absent_from_result(self, session: Session) -> None:
        _add_underlying(session, "META")
        _add_contract(session, contract_ticker="META240619C00300000", underlying="META")
        session.commit()

        repo = SqlDistillationRepository(session)
        pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )

        # Contract exists but has no snapshots — it is not surfaced here;
        # the q3 loader composes the absence with the contracts list.
        assert pairs == {}

    def test_other_underlying_snapshots_are_excluded(self, session: Session) -> None:
        _add_underlying(session, "META")
        _add_underlying(session, "AAPL")
        _add_contract(session, contract_ticker="META240619C00100000", underlying="META")
        _add_contract(session, contract_ticker="AAPL240619C00150000", underlying="AAPL")
        _add_snapshot(
            session,
            contract_ticker="META240619C00100000",
            underlying="META",
            snapshot_ts="2026-05-15T00:00:00Z",
        )
        _add_snapshot(
            session,
            contract_ticker="AAPL240619C00150000",
            underlying="AAPL",
            snapshot_ts="2026-05-15T00:00:00Z",
        )
        session.commit()

        repo = SqlDistillationRepository(session)
        meta_pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )
        aapl_pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("AAPL"), as_of="2026-05-15T00:00:00Z"
        )

        assert set(meta_pairs.keys()) == {"META240619C00100000"}
        assert set(aapl_pairs.keys()) == {"AAPL240619C00150000"}

    def test_prior_is_most_recent_strictly_before_as_of(self, session: Session) -> None:
        _add_underlying(session, "META")
        _add_contract(session, contract_ticker="META240619C00100000", underlying="META")
        # Three priors plus today; prior must be the newest pre-as_of row.
        for ts, vol in [
            ("2026-05-01T00:00:00Z", 10),
            ("2026-05-05T00:00:00Z", 20),
            ("2026-05-12T00:00:00Z", 30),
            ("2026-05-15T00:00:00Z", 100),  # today
        ]:
            _add_snapshot(
                session,
                contract_ticker="META240619C00100000",
                underlying="META",
                snapshot_ts=ts,
                volume_today=vol,
            )
        session.commit()

        repo = SqlDistillationRepository(session)
        pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )

        today, prior = pairs["META240619C00100000"]
        assert today is not None and today.volume_today == 100
        assert prior is not None
        assert prior.snapshot_ts == "2026-05-12T00:00:00Z"
        assert prior.volume_today == 30

    def test_null_volume_and_open_interest_preserve_none(self, session: Session) -> None:
        _add_underlying(session, "META")
        _add_contract(session, contract_ticker="META240619C00100000", underlying="META")
        _add_snapshot(
            session,
            contract_ticker="META240619C00100000",
            underlying="META",
            snapshot_ts="2026-05-15T00:00:00Z",
            volume_today=None,
            open_interest=None,
        )
        session.commit()

        repo = SqlDistillationRepository(session)
        pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )
        today, prior = pairs["META240619C00100000"]
        assert today is not None
        assert today.volume_today is None
        assert today.open_interest is None
        assert prior is None

    def test_multiple_contracts_yield_per_contract_pairs(self, session: Session) -> None:
        _add_underlying(session, "META")
        # A: both today + prior
        _add_contract(session, contract_ticker="META240619C00100000", underlying="META")
        _add_snapshot(
            session,
            contract_ticker="META240619C00100000",
            underlying="META",
            snapshot_ts="2026-05-10T00:00:00Z",
        )
        _add_snapshot(
            session,
            contract_ticker="META240619C00100000",
            underlying="META",
            snapshot_ts="2026-05-15T00:00:00Z",
        )
        # B: today only
        _add_contract(session, contract_ticker="META240619C00200000", underlying="META")
        _add_snapshot(
            session,
            contract_ticker="META240619C00200000",
            underlying="META",
            snapshot_ts="2026-05-15T00:00:00Z",
        )
        # C: prior only
        _add_contract(session, contract_ticker="META240619P00100000", underlying="META")
        _add_snapshot(
            session,
            contract_ticker="META240619P00100000",
            underlying="META",
            snapshot_ts="2026-05-12T00:00:00Z",
        )
        # D: neither (should be absent)
        _add_contract(session, contract_ticker="META240619P00200000", underlying="META")
        session.commit()

        repo = SqlDistillationRepository(session)
        pairs = repo.load_options_snapshot_pairs_for_underlying(
            underlying=Symbol("META"), as_of="2026-05-15T00:00:00Z"
        )

        assert set(pairs.keys()) == {
            "META240619C00100000",
            "META240619C00200000",
            "META240619P00100000",
        }
        a_today, a_prior = pairs["META240619C00100000"]
        assert a_today is not None and a_prior is not None
        b_today, b_prior = pairs["META240619C00200000"]
        assert b_today is not None and b_prior is None
        c_today, c_prior = pairs["META240619P00100000"]
        assert c_today is None and c_prior is not None
