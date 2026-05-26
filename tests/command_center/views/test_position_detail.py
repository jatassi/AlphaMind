"""Tests for ``command_center.views.portfolio`` position-detail endpoint (ALP-677).

GET /api/views/portfolio/positions/{position_id}

Scoped pytest: ``uv run pytest tests/command_center/views/ -n auto``.

All tests use an on-disk tmp_path SQLite (not :memory:) because
``build_foreign_reader_session_factory`` rejects ``:memory:`` with
``?mode=ro`` — SQLite requires an on-disk file for read-only mode.

The fixture seeds position + bracket + bracket_legs + fill + thesis +
thesis_components + activity_log rows, then exercises the endpoint via
FastAPI's ``TestClient``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

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


async def _create_tables(engine: object) -> None:
    async with engine.begin() as conn:  # type: ignore[union-attr]
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
    db_file = tmp_path / "test_position_detail.db"
    db_file.touch()
    return str(db_file)


@pytest.fixture
async def seeded_db(db_path: str) -> AsyncIterator[str]:
    """Create schema + seed a rich position; yield db_path."""
    writer_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(writer_url)
    await _create_tables(engine)

    async with engine.begin() as conn:
        pos_details = json.dumps(
            {
                "ticker": "TSLA",
                "quantity": 50,
                "market_value_usd": "12000.00",
                "unrealized_pnl_usd": "750.00",
                "sector": "Consumer Discretionary",
                "distance_to_target_pct": 8.5,
                "distance_to_nearest_invalidation_pct": -4.2,
            }
        )
        # Position (bracket/thesis FKs are DEFERRED so we can insert freely).
        await conn.execute(
            text(
                "INSERT INTO positions"
                "    (position_id, status, instrument_type, direction, details_json,"
                "     execution_history_json, corporate_action_adjustment_needed,"
                "     entry_timestamp, bracket_id)"
                " VALUES"
                "    ('pos-detail-1', 'OPEN', 'EQUITY', 'LONG', :details, '[]', 0,"
                "     '2026-05-20T09:00:00Z', 'brk-detail-1')"
            ),
            {"details": pos_details},
        )

        # Order (needed for fill FK and bracket entry_order_id).
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
                    ('ord-detail-1', 'pos-detail-1', 'brk-detail-1', 'ENTRY', 'SIMPLE',
                     '{"ticker":"TSLA"}', 'MARKET', 50.0, '{}',
                     'GTC', 'FILLED', 'alp-ord-d1', '[]',
                     '2026-05-20T09:00:00Z', '2026-05-20T09:01:00Z',
                     50.0, 0.0, 0, '{}')
            """)
        )

        # Bracket (entry_order_id → orders, deferred).
        await conn.execute(
            text("""
                INSERT INTO brackets
                    (bracket_id, position_id, status, entry_order_id,
                     modification_history_json)
                VALUES
                    ('brk-detail-1', 'pos-detail-1', 'ACTIVE', 'ord-detail-1', '[]')
            """)
        )

        # Bracket leg.
        await conn.execute(
            text("""
                INSERT INTO bracket_legs
                    (bracket_leg_id, bracket_id, leg_index, leg_type, trigger_kind,
                     trigger_payload_json, enforcement, leg_status)
                VALUES
                    ('leg-detail-1', 'brk-detail-1', 0, 'PRICE_STOP', 'PRICE',
                     '{"trigger_price": "200.00"}', 'MECHANICAL', 'ACTIVE')
            """)
        )

        # Fill.
        await conn.execute(
            text("""
                INSERT INTO fill_records
                    (fill_id, order_id, fill_timestamp, fill_price,
                     fill_quantity, remaining_quantity_after, order_status_after,
                     fees_usd, persistence_timestamp, processing_status)
                VALUES
                    ('fill-detail-1', 'ord-detail-1', '2026-05-20T09:01:00Z', '220.00',
                     50.0, 0.0, 'FILLED', '1.50',
                     '2026-05-20T09:01:01Z', 'processed')
            """)
        )

        # Thesis.
        await conn.execute(
            text("""
                INSERT INTO theses
                    (thesis_id, position_id, status, summary,
                     generation_timestamp, narrative_json)
                VALUES
                    ('ths-detail-1', 'pos-detail-1', 'ACTIVE',
                     'Bullish on TSLA momentum',
                     '2026-05-20T08:55:00Z', '{}')
            """)
        )

        # Thesis component — entry rationale.
        await conn.execute(
            text("""
                INSERT INTO thesis_components
                    (component_id, thesis_id, component_type, narrative,
                     key_assumptions_json, supporting_signals_json)
                VALUES
                    ('comp-detail-1', 'ths-detail-1', 'ENTRY_RATIONALE',
                     'Strong momentum with breakout above key resistance',
                     '["Volume confirmation", "RSI > 60"]',
                     '["signal:momentum_breakout"]')
            """)
        )

        # Activity log entry for this position.
        await conn.execute(
            text("""
                INSERT INTO activity_log
                    (entry_id, invocation_id, entry_at, event_type, event_group,
                     position_id, source, detail_json)
                VALUES
                    ('log-detail-1', 'inv-1', '2026-05-20T09:01:05Z',
                     'POSITION_OPENED', 'POSITION_LIFECYCLE',
                     'pos-detail-1', 'FILL_PROCESSOR',
                     '{"description": "TSLA LONG opened"}')
            """)
        )

    await engine.dispose()
    yield db_path


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPositionDetailEndpoint:
    """GET /api/views/portfolio/positions/{position_id}."""

    def test_returns_200_for_existing_position(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/positions/pos-detail-1")
        assert resp.status_code == 200

    def test_returns_404_for_missing_position(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/positions/nonexistent-pos")
        assert resp.status_code == 404

    def test_position_fields(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            body = client.get("/api/views/portfolio/positions/pos-detail-1").json()
        assert body["position_id"] == "pos-detail-1"
        assert body["ticker"] == "TSLA"
        assert body["instrument_type"] == "EQUITY"
        assert body["direction"] == "LONG"
        assert body["status"] == "OPEN"
        assert body["quantity"] == pytest.approx(50.0)
        assert body["market_value_usd"] == "12000.00"
        assert body["unrealized_pl_usd"] == "750.00"
        assert body["distance_to_target_pct"] == pytest.approx(8.5)
        assert body["distance_to_nearest_invalidation_pct"] == pytest.approx(-4.2)
        assert body["bracket_id"] == "brk-detail-1"

    def test_bracket_legs(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            body = client.get("/api/views/portfolio/positions/pos-detail-1").json()
        legs = body["bracket_legs"]
        assert len(legs) == 1
        leg = legs[0]
        assert leg["bracket_leg_id"] == "leg-detail-1"
        assert leg["leg_type"] == "PRICE_STOP"
        assert leg["trigger_kind"] == "PRICE"
        assert leg["leg_status"] == "ACTIVE"
        assert leg["enforcement"] == "MECHANICAL"
        assert leg["trigger_payload"]["trigger_price"] == "200.00"

    def test_fills(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            body = client.get("/api/views/portfolio/positions/pos-detail-1").json()
        fills = body["fills"]
        assert len(fills) == 1
        fill = fills[0]
        assert fill["fill_id"] == "fill-detail-1"
        assert fill["order_id"] == "ord-detail-1"
        assert fill["fill_price"] == "220.00"
        assert fill["fill_quantity"] == pytest.approx(50.0)
        assert fill["fees_usd"] == "1.50"

    def test_thesis_and_components(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            body = client.get("/api/views/portfolio/positions/pos-detail-1").json()
        thesis = body["thesis"]
        assert thesis is not None
        assert thesis["thesis_id"] == "ths-detail-1"
        assert thesis["status"] == "ACTIVE"
        assert thesis["summary"] == "Bullish on TSLA momentum"
        components = thesis["components"]
        assert len(components) == 1
        comp = components[0]
        assert comp["component_type"] == "ENTRY_RATIONALE"
        assert "Volume confirmation" in comp["key_assumptions"]
        assert "RSI > 60" in comp["key_assumptions"]
        assert "signal:momentum_breakout" in comp["supporting_signals"]

    def test_activity_log_prefiltered(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            body = client.get("/api/views/portfolio/positions/pos-detail-1").json()
        entries = body["activity_log"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["entry_id"] == "log-detail-1"
        assert entry["event_type"] == "POSITION_OPENED"
        assert entry["source"] == "FILL_PROCESSOR"

    async def test_no_thesis_returns_null(self, db_path: str) -> None:
        """A position without a thesis returns thesis: null."""
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        await _create_tables(engine)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO positions"
                    "    (position_id, status, instrument_type, direction, details_json,"
                    "     execution_history_json, corporate_action_adjustment_needed)"
                    " VALUES"
                    "    ('pos-no-thesis', 'OPEN', 'EQUITY', 'LONG',"
                    "     '{\"ticker\": \"NVDA\"}', '[]', 0)"
                )
            )
        await engine.dispose()

        reader = build_foreign_reader_session_factory(db_path)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            body = client.get("/api/views/portfolio/positions/pos-no-thesis").json()
        assert body["thesis"] is None
        assert body["bracket_legs"] == []
        assert body["fills"] == []
        assert body["activity_log"] == []
