"""Tests for iv_lookup.py (ALP-561).

Covers:
* ``resolve_contract_ticker`` — OCC encoding for call and put contracts.
* ``lookup_iv_at_timestamp`` — happy-path, no-snapshot miss, IV-null miss,
  exact-timestamp match (lag=0), 30-min lag, 90-min lag.
* ``SqlOptionsSnapshotRepository`` — implements the widened Protocol; the three
  methods are tested through the repo surface rather than the bare functions.

The database boundary is real (SQLite in-memory via make_engine / Base.metadata);
no internal collaborators are mocked.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — pre-import to avoid latent circular import
from alphamind.execution.counterfactual_replay_engine.iv_lookup import (
    IVSnapshotLookupResult,
    SqlOptionsSnapshotRepository,
    resolve_contract_ticker,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_UNDERLYING = "AAPL"
_STRIKE = Decimal("150.000")
_EXPIRY = date(2026, 6, 20)
# O:AAPL260620C00150000 — 150.000 * 1000 = 150000, zero-padded to 8 digits
_CALL_TICKER = "O:AAPL260620C00150000"
_PUT_TICKER = "O:AAPL260620P00150000"

_SNAP_TS = datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC)
_TARGET_TS_EXACT = _SNAP_TS
_TARGET_TS_30 = datetime(2026, 4, 15, 10, 30, 0, tzinfo=UTC)
_TARGET_TS_90 = datetime(2026, 4, 15, 11, 30, 0, tzinfo=UTC)
_TARGET_BEFORE = datetime(2026, 4, 15, 9, 0, 0, tzinfo=UTC)  # before snapshot

_IV = 0.32
_UNDERLYING_PRICE = 148.50


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(_engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(_engine)
    with factory() as s:
        # Seed the asset_universe parent row (required by FK chain)
        s.add(
            AssetUniverse(
                asset_id="AAPL-ID",
                ticker=_UNDERLYING,
                full_name="Apple Inc.",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2020-01-01",
            )
        )
        s.flush()
        yield s


def _add_contract(session: Session, *, contract_ticker: str, contract_type: str = "call") -> None:
    """Insert a minimal OptionsContracts row."""
    session.add(
        OptionsContracts(
            contract_ticker=contract_ticker,
            underlying_ticker=_UNDERLYING,
            expiration_date=_EXPIRY.isoformat(),
            strike_price=float(_STRIKE),
            contract_type=contract_type,
            first_seen_at=_SNAP_TS.isoformat(),
            last_seen_at=_SNAP_TS.isoformat(),
            source="test",
        )
    )
    session.flush()


def _add_snapshot(
    session: Session,
    *,
    contract_ticker: str,
    snapshot_ts: datetime,
    implied_volatility: float | None,
    underlying_price: float | None = _UNDERLYING_PRICE,
) -> None:
    """Insert a minimal OptionsContractSnapshots row."""
    session.add(
        OptionsContractSnapshots(
            snapshot_ts=snapshot_ts.isoformat(),
            contract_ticker=contract_ticker,
            underlying_ticker=_UNDERLYING,
            implied_volatility=implied_volatility,
            underlying_price=underlying_price,
            source="test",
            ingested_at=snapshot_ts.isoformat(),
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# resolve_contract_ticker — pure function, no session
# ---------------------------------------------------------------------------


class TestResolveContractTicker:
    def test_call_produces_canonical_occ_symbol(self) -> None:
        result = resolve_contract_ticker(
            underlying=_UNDERLYING,
            strike=_STRIKE,
            expiration=_EXPIRY,
            contract_type="call",
        )
        assert result == _CALL_TICKER

    def test_put_produces_canonical_occ_symbol(self) -> None:
        result = resolve_contract_ticker(
            underlying=_UNDERLYING,
            strike=_STRIKE,
            expiration=_EXPIRY,
            contract_type="put",
        )
        assert result == _PUT_TICKER

    def test_strike_rounded_not_truncated(self) -> None:
        # 149.999 * 1000 = 149999 → rounds to 150000 (not 149999 if truncated)
        # This verifies round() semantics.
        result = resolve_contract_ticker(
            underlying="MSFT",
            strike=Decimal("149.9995"),  # rounds up to 150000
            expiration=date(2026, 12, 19),
            contract_type="call",
        )
        # 149.9995 * 1000 = 149999.5 → round() → 150000
        assert result == "O:MSFT261219C00150000"

    def test_o_prefix_present(self) -> None:
        result = resolve_contract_ticker(
            underlying="SPY",
            strike=Decimal("500.00"),
            expiration=date(2026, 1, 16),
            contract_type="put",
        )
        assert result.startswith("O:")


# ---------------------------------------------------------------------------
# SqlOptionsSnapshotRepository — exercises all three protocol methods
# ---------------------------------------------------------------------------


class TestHasSnapshotAtOrBefore:
    """has_snapshot_at_or_before only counts non-null IV rows."""

    def test_returns_true_when_non_null_snapshot_exists(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        assert repo.has_snapshot_at_or_before(_CALL_TICKER, _TARGET_TS_30) is True

    def test_returns_false_when_no_snapshot_before_target(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        # target is before the snapshot
        assert repo.has_snapshot_at_or_before(_CALL_TICKER, _TARGET_BEFORE) is False

    def test_returns_false_when_snapshot_exists_but_iv_is_null(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=None
        )
        repo = SqlOptionsSnapshotRepository(session)
        assert repo.has_snapshot_at_or_before(_CALL_TICKER, _TARGET_TS_30) is False

    def test_returns_false_for_unknown_contract(self, session: Session) -> None:
        repo = SqlOptionsSnapshotRepository(session)
        assert repo.has_snapshot_at_or_before("O:UNKNOWN260620C00100000", _TARGET_TS_30) is False


class TestResolveContractTickerViaRepo:
    """resolve_contract_ticker is also surfaced on the repo for protocol compliance."""

    def test_repo_delegates_to_pure_function(self, session: Session) -> None:
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.resolve_contract_ticker(
            underlying=_UNDERLYING,
            strike=_STRIKE,
            expiration=_EXPIRY,
            contract_type="call",
        )
        assert result == _CALL_TICKER


class TestLookupIV:
    """lookup_iv returns IVSnapshotLookupResult or None."""

    def test_happy_path_returns_result(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session,
            contract_ticker=_CALL_TICKER,
            snapshot_ts=_SNAP_TS,
            implied_volatility=_IV,
            underlying_price=_UNDERLYING_PRICE,
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_30)
        assert result is not None
        assert isinstance(result, IVSnapshotLookupResult)
        assert result.implied_volatility == _IV
        assert result.underlying_price_at_snapshot == _UNDERLYING_PRICE

    def test_exact_timestamp_match_gives_zero_lag(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_EXACT)
        assert result is not None
        assert result.lag_minutes == 0.0

    def test_30_minute_lag(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_30)
        assert result is not None
        assert result.lag_minutes == 30.0

    def test_90_minute_lag(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_90)
        assert result is not None
        assert result.lag_minutes == 90.0

    def test_no_snapshot_before_target_returns_none(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_BEFORE)
        assert result is None

    def test_iv_null_snapshot_returns_none(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=None
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_30)
        assert result is None

    def test_unknown_contract_returns_none(self, session: Session) -> None:
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker="O:UNKNOWN260620C00100000", target_ts=_TARGET_TS_30)
        assert result is None

    def test_snapshot_ts_on_result_matches_db_row(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_30)
        assert result is not None
        assert result.snapshot_ts == _SNAP_TS

    def test_selects_nearest_snapshot_when_multiple_exist(self, session: Session) -> None:
        """Picks the latest snapshot at-or-before target, not an earlier one."""
        earlier_ts = datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC)
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=earlier_ts, implied_volatility=0.25
        )
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=_IV
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_30)
        assert result is not None
        assert result.implied_volatility == _IV  # not the earlier 0.25
        assert result.lag_minutes == 30.0

    def test_iv_null_row_skipped_picks_earlier_non_null(self, session: Session) -> None:
        """If the nearest snapshot has null IV, falls back to earlier non-null."""
        earlier_ts = datetime(2026, 4, 15, 8, 0, 0, tzinfo=UTC)
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=earlier_ts, implied_volatility=0.28
        )
        _add_snapshot(
            session, contract_ticker=_CALL_TICKER, snapshot_ts=_SNAP_TS, implied_volatility=None
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_30)
        assert result is not None
        assert result.implied_volatility == 0.28
        # lag = target (10:30) - earlier (08:00) = 150 min
        assert result.lag_minutes == 150.0

    def test_underlying_price_none_when_db_null(self, session: Session) -> None:
        _add_contract(session, contract_ticker=_CALL_TICKER)
        _add_snapshot(
            session,
            contract_ticker=_CALL_TICKER,
            snapshot_ts=_SNAP_TS,
            implied_volatility=_IV,
            underlying_price=None,
        )
        repo = SqlOptionsSnapshotRepository(session)
        result = repo.lookup_iv(contract_ticker=_CALL_TICKER, target_ts=_TARGET_TS_30)
        assert result is not None
        assert result.underlying_price_at_snapshot is None
