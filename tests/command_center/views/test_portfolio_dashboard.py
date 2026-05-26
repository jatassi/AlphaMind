"""Tests for ``command_center.views.portfolio`` (ALP-676).

Scoped pytest: ``uv run pytest tests/command_center/views/ -n auto``.

All tests use an on-disk tmp_path SQLite (not :memory:) because
``build_foreign_reader_session_factory`` rejects ``:memory:`` with
``?mode=ro`` — SQLite requires an on-disk file for read-only mode.

The fixture seeds both the production-schema tables (via
``alphamind.persistence.models.Base``) and reads back via the
command-center foreign-reader session factory so the endpoint logic
is exercised end-to-end.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center.persistence.session import (
    build_foreign_reader_session_factory,
)
from alphamind.command_center.views.portfolio import build_portfolio_router
from alphamind.persistence.models import Base

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_tables(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _app_with_reader(reader_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    app.state.foreign_reader_session_factory = reader_factory
    app.include_router(build_portfolio_router(), prefix="/api/views/portfolio")
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Return path to a fresh on-disk SQLite file with the production schema."""
    db_file = tmp_path / "test_portfolio.db"
    db_file.touch()
    return str(db_file)


@pytest.fixture
async def seeded_db(db_path: str) -> AsyncIterator[str]:
    """Create schema + seed minimal rows; yield db_path."""
    writer_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(writer_url)
    await _create_tables(engine)

    async with engine.begin() as conn:
        # Seed drawdown_state singleton.
        await conn.execute(
            text("""
                INSERT INTO drawdown_state
                    (id, equity_high_water_mark_usd, current_drawdown_pct,
                     drawdown_duration_hours, lifetime_max_drawdown_pct,
                     drawdown_by_source_json, last_updated_at)
                VALUES
                    ('current', 100000.0, 2.5, 24.0, 5.0, '{}',
                     '2026-05-26T00:00:00Z')
            """)
        )

        # Seed cash_ledger singleton.
        await conn.execute(
            text("""
                INSERT INTO cash_ledger
                    (id, current_cash_usd, settled_cash_usd, reserved_capital_usd,
                     available_buying_power_usd, margin_held_usd,
                     unsettled_proceeds_json, last_updated_at)
                VALUES
                    ('current', '50000.00', '48000.00', '5000.00',
                     '43000.00', '2000.00', '[]',
                     '2026-05-26T00:00:00Z')
            """)
        )

        # Seed a position + thesis.
        pos_details = json.dumps(
            {
                "ticker": "AAPL",
                "quantity": 100,
                "market_value_usd": "15000.00",
                "unrealized_pnl_usd": "500.00",
                "sector": "Technology",
            }
        )
        await conn.execute(
            text(
                "INSERT INTO positions"
                "    (position_id, status, instrument_type, direction, details_json,"
                "     execution_history_json, corporate_action_adjustment_needed)"
                " VALUES"
                "    ('pos-1', 'OPEN', 'EQUITY', 'LONG', :details, '[]', 0)"
            ),
            {"details": pos_details},
        )
        await conn.execute(
            text("""
                INSERT INTO theses
                    (thesis_id, position_id, status, summary,
                     generation_timestamp, narrative_json)
                VALUES
                    ('ths-1', 'pos-1', 'ACTIVE', 'Bull thesis on AAPL',
                     '2026-05-26T00:00:00Z', '{}')
            """)
        )

        # Seed a pending order.  The brackets↔orders FK cycle (brackets →
        # entry_order_id → orders → bracket_id → brackets) requires both
        # FKs to be DEFERRABLE INITIALLY DEFERRED.  We insert both rows in
        # the same transaction and the deferred FKs resolve at COMMIT.
        await conn.execute(
            text("""
                INSERT INTO orders
                    (order_id, position_id, bracket_id, order_role, order_class,
                     instrument_spec_json, order_type, quantity, price_parameters_json,
                     duration, status, alpaca_order_id, alpaca_order_id_chain_json,
                     submission_timestamp, last_update_timestamp,
                     filled_quantity, remaining_quantity, modification_count,
                     metadata_json)
                VALUES
                    ('ord-1', 'pos-1', 'brk-1', 'ENTRY', 'SIMPLE',
                     '{"ticker":"AAPL"}', 'MARKET', 100.0, '{}',
                     'GTC', 'PENDING', 'alp-ord-1', '[]',
                     '2026-05-26T00:00:00Z', '2026-05-26T00:00:00Z',
                     0.0, 100.0, 0, '{}')
            """)
        )
        await conn.execute(
            text("""
                INSERT INTO brackets
                    (bracket_id, position_id, status, entry_order_id,
                     modification_history_json)
                VALUES
                    ('brk-1', 'pos-1', 'ACTIVE', 'ord-1', '[]')
            """)
        )

        # Seed a fill record with regt attribution.
        regt_json = json.dumps(
            {
                "regt_margin_before": 10000.0,
                "regt_margin_after": 12000.0,
                "regt_marginal_consumption": 2000.0,
                "pm_equivalent_before": 8000.0,
                "pm_equivalent_after": 9500.0,
                "pm_marginal_consumption": 1500.0,
                "regt_excess_over_pm": 500.0,
                "pm_model_version": "occ_tims_v1_2026Q2",
            }
        )
        await conn.execute(
            text("""
                INSERT INTO fill_records
                    (fill_id, order_id, fill_timestamp, fill_price,
                     fill_quantity, remaining_quantity_after, order_status_after,
                     fees_usd, persistence_timestamp, processing_status,
                     regt_attribution_json)
                VALUES
                    ('fill-1', 'ord-1', '2026-05-26T10:00:00Z', '150.00',
                     100.0, 0.0, 'FILLED', '0.00',
                     '2026-05-26T10:00:01Z', 'processed',
                     :regt_json)
            """),
            {"regt_json": regt_json},
        )

    await engine.dispose()
    yield db_path


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPortfolioDashboardEndpoint:
    """GET /api/views/portfolio/dashboard — full five-pane response."""

    def test_returns_200_all_five_panes(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert "equity_and_pl" in body
        assert "cash_and_capital" in body
        assert "exposure" in body
        assert "positions" in body
        assert "pending_orders" in body

    def test_equity_and_pl_pane(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        pane = resp.json()["equity_and_pl"]
        # HWM comes from drawdown_state row (100000.0).
        assert Decimal(pane["high_water_mark_usd"]) == Decimal(100000)
        assert pane["current_drawdown_pct"] == pytest.approx(2.5)

    def test_cash_and_capital_pane(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        pane = resp.json()["cash_and_capital"]
        assert Decimal(pane["cash_usd"]) == Decimal("50000.00")
        assert Decimal(pane["settled_cash_usd"]) == Decimal("48000.00")
        assert Decimal(pane["reserved_capital_usd"]) == Decimal("5000.00")
        assert Decimal(pane["available_buying_power_usd"]) == Decimal("43000.00")
        assert Decimal(pane["margin_held_usd"]) == Decimal("2000.00")

    def test_regt_excess_trailing_computed(self, seeded_db: str) -> None:
        """Reg T excess accumulates from fill_records.regt_attribution_json."""
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        regt = resp.json()["cash_and_capital"]["regt_excess"]
        # Seeded fill has regt_excess_over_pm = 500.0.
        assert Decimal(regt["lifetime_usd"]) == Decimal(500)
        # The fill is within 30d and 90d windows.
        assert Decimal(regt["trailing_30d_usd"]) == Decimal(500)
        assert Decimal(regt["trailing_90d_usd"]) == Decimal(500)

    def test_positions_pane(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        positions = resp.json()["positions"]
        assert len(positions) == 1
        pos = positions[0]
        assert pos["position_id"] == "pos-1"
        assert pos["ticker"] == "AAPL"
        assert pos["instrument_type"] == "EQUITY"
        assert pos["thesis_status"] == "ACTIVE"
        assert Decimal(pos["market_value_usd"]) == Decimal("15000.00")
        assert Decimal(pos["unrealized_pl_usd"]) == Decimal("500.00")

    def test_pending_orders_pane(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        orders = resp.json()["pending_orders"]
        assert len(orders) == 1
        order = orders[0]
        assert order["order_id"] == "ord-1"
        assert order["status"] == "PENDING"
        assert order["quantity"] == pytest.approx(100.0)

    def test_empty_db_returns_200(self, db_path: str) -> None:
        """An empty DB (tables exist, no rows) returns 200 with empty lists."""
        # Create tables but seed no rows.
        import asyncio

        async def _create() -> None:
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
            await _create_tables(engine)
            await engine.dispose()

        asyncio.run(_create())
        reader = build_foreign_reader_session_factory(db_path)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["positions"] == []
        assert body["pending_orders"] == []

    def test_exposure_pane_sector_breakdown(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/dashboard")
        sectors = resp.json()["exposure"]["sector_breakdown"]
        sector_names = [s["sector"] for s in sectors]
        # Seeded position has sector=Technology.
        assert "Technology" in sector_names
