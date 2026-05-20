"""Tests for the borrow-cost resolver and daily-accrual conversion (ALP-586)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import Table
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.persistence.models import AssetUniverse, Base, BorrowCostDaily
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.risk_guardrails.borrow_cost import (
    build_borrow_cost_resolver,
    daily_borrow_cost_usd,
)

_INGESTED_AT = datetime(2026, 5, 19, tzinfo=UTC).isoformat()


# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Iterator[Session]:
    engine: Engine = make_engine(":memory:")
    # Create only the two tables this suite touches — creating the full
    # Base.metadata would pull unrelated tables whose foreign keys reference
    # state-layer tables not registered by importing persistence.models alone.
    Base.metadata.create_all(
        engine,
        tables=[
            cast(Table, AssetUniverse.__table__),
            cast(Table, BorrowCostDaily.__table__),
        ],
    )
    session_factory = make_session_factory(engine)
    with session_factory() as sess:
        yield sess
    engine.dispose()


def _add_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Corp",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2026-01-01",
            last_updated=_INGESTED_AT,
        )
    )
    session.flush()


def _add_borrow_row(
    session: Session,
    ticker: str,
    *,
    observation_date: str,
    fee_pct: float | None,
) -> None:
    session.add(
        BorrowCostDaily(
            observation_date=observation_date,
            ticker=ticker,
            fee_pct=fee_pct,
            source="test",
            ingested_at=_INGESTED_AT,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# daily_borrow_cost_usd — annualized rate → one-day USD accrual
# ---------------------------------------------------------------------------


def test_daily_borrow_cost_usd_converts_annual_rate_to_one_day_accrual() -> None:
    """A 25.2% annualized fee on $100k notional accrues $100/day over 252 days."""
    cost = daily_borrow_cost_usd(notional_usd=100_000.0, annual_fee_pct=25.2)
    assert cost == pytest.approx(100_000.0 * 0.252 / 252.0)
    assert cost == pytest.approx(100.0)


def test_daily_borrow_cost_usd_scales_linearly_with_notional() -> None:
    """Doubling the notional doubles the daily accrual at a fixed fee rate."""
    small = daily_borrow_cost_usd(notional_usd=5_000.0, annual_fee_pct=12.0)
    large = daily_borrow_cost_usd(notional_usd=10_000.0, annual_fee_pct=12.0)
    assert large == pytest.approx(2.0 * small)


def test_daily_borrow_cost_usd_zero_fee_is_zero() -> None:
    """A zero annualized fee accrues zero daily cost regardless of notional."""
    assert daily_borrow_cost_usd(notional_usd=50_000.0, annual_fee_pct=0.0) == 0.0


# ---------------------------------------------------------------------------
# build_borrow_cost_resolver — snapshot-backed ticker → annualized fee lookup
# ---------------------------------------------------------------------------


def test_resolver_returns_fee_pct_for_covered_ticker(session: Session) -> None:
    """A ticker with a borrow_cost_daily row resolves to that row's fee_pct."""
    _add_ticker(session, "GME")
    _add_borrow_row(session, "GME", observation_date="2026-05-19", fee_pct=42.5)

    resolver = build_borrow_cost_resolver(session)
    assert resolver("GME") == pytest.approx(42.5)


def test_resolver_returns_none_for_uncovered_ticker(session: Session) -> None:
    """A ticker with no borrow_cost_daily row resolves to None, not 0.0 — the
    coverage-gap signal that distinguishes 'no data' from a genuine zero fee."""
    _add_ticker(session, "GME")
    _add_borrow_row(session, "GME", observation_date="2026-05-19", fee_pct=42.5)

    resolver = build_borrow_cost_resolver(session)
    assert resolver("CSCO") is None


def test_resolver_uses_latest_observation_date(session: Session) -> None:
    """When a ticker has multiple rows, the resolver returns the most recent
    observation_date's fee_pct, not an earlier one."""
    _add_ticker(session, "GME")
    _add_borrow_row(session, "GME", observation_date="2026-05-17", fee_pct=10.0)
    _add_borrow_row(session, "GME", observation_date="2026-05-19", fee_pct=55.0)
    _add_borrow_row(session, "GME", observation_date="2026-05-18", fee_pct=30.0)

    resolver = build_borrow_cost_resolver(session)
    assert resolver("GME") == pytest.approx(55.0)


def test_resolver_treats_null_latest_fee_as_uncovered(session: Session) -> None:
    """A latest row with a NULL fee_pct (iBorrowDesk returned no fee that day)
    resolves to None — a stale earlier fee is not served as if current."""
    _add_ticker(session, "GME")
    _add_borrow_row(session, "GME", observation_date="2026-05-17", fee_pct=10.0)
    _add_borrow_row(session, "GME", observation_date="2026-05-19", fee_pct=None)

    resolver = build_borrow_cost_resolver(session)
    assert resolver("GME") is None
