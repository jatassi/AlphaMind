"""Shared asset_universe query primitives (ALP-533).

Consolidates the SELECT statement and ``int | None → float | None`` coercion
previously duplicated between ``SqlDistillationRepository.load_ticker_adv``
(sync session) and ``SqlAdvLookup.get_adv_shares`` (async session). The
helpers are sqlalchemy-bearing but session-agnostic — each adapter executes
the returned ``Select`` against its own session shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from alphamind.persistence.asset_universe_queries import (
    adv_shares_select,
    coerce_adv_shares,
)
from alphamind.persistence.models import AssetUniverse, Base
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def session(tmp_path: Path) -> Iterator[Session]:
    import alphamind.state.tables  # noqa: F401 — register ORM mappings (FK targets)

    engine = make_engine(str(tmp_path / "alphamind.db"))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess
    engine.dispose()


def _add_asset(session: Session, *, ticker: str, adv: int | None) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker}",
            ticker=ticker,
            full_name=ticker,
            asset_class="EQUITY",
            asset_role="UNIVERSE",
            exchange="NASDAQ",
            cik=None,
            figi=None,
            isin=None,
            shares_outstanding=None,
            float_shares=None,
            market_cap_usd=None,
            avg_daily_volume_shares=adv,
            avg_daily_volume_notional_usd=None,
            beta_spy=None,
            analyst_count=None,
            options_chain_liquid=None,
            ipo_date=None,
            is_active=1,
            added_date="2024-01-01",
            removed_date=None,
            removal_reason=None,
            last_updated="2024-01-01",
        )
    )
    session.commit()


class TestCoerceAdvShares:
    def test_none_passes_through(self) -> None:
        assert coerce_adv_shares(None) is None

    def test_zero_coerces_to_float_zero(self) -> None:
        result = coerce_adv_shares(0)
        assert result == 0.0
        assert isinstance(result, float)

    def test_positive_int_coerces_to_float(self) -> None:
        result = coerce_adv_shares(50_000_000)
        assert result == 50_000_000.0
        assert isinstance(result, float)


class TestAdvSharesSelect:
    """The shared SELECT statement against ``asset_universe`` — exercised via
    both sync and async sessions in the real adapters; the substrate-level
    behaviour is identical regardless of session shape."""

    def test_present_ticker_returns_adv(self, session: Session) -> None:
        _add_asset(session, ticker="AAPL", adv=50_000_000)
        row = session.execute(adv_shares_select("AAPL")).one_or_none()
        assert row is not None
        assert coerce_adv_shares(row[0]) == 50_000_000.0

    def test_null_adv_returns_row_with_none_column(self, session: Session) -> None:
        _add_asset(session, ticker="NULLVOL", adv=None)
        row = session.execute(adv_shares_select("NULLVOL")).one_or_none()
        assert row is not None
        assert coerce_adv_shares(row[0]) is None

    def test_missing_ticker_returns_none_row(self, session: Session) -> None:
        row = session.execute(adv_shares_select("UNKNOWN")).one_or_none()
        assert row is None
