"""Tests for theses endpoints in command_center.views.portfolio (ALP-678).

Scoped pytest: ``uv run pytest tests/command_center/views/test_theses.py -n auto``.

All tests use an on-disk tmp_path SQLite (not :memory:) because
``build_foreign_reader_session_factory`` rejects ``:memory:`` with
``?mode=ro`` — SQLite requires an on-disk file for read-only mode.

The fixture seeds positions, theses, thesis_components, and activity_log
rows, then exercises the two new endpoints:

  GET /api/views/portfolio/theses
  GET /api/views/portfolio/theses/{thesis_id}
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
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
    """Return path to a fresh on-disk SQLite file."""
    db_file = tmp_path / "test_theses.db"
    db_file.touch()
    return str(db_file)


@pytest.fixture
async def seeded_db(db_path: str) -> AsyncIterator[str]:
    """Create schema + seed theses, components, and activity_log rows."""
    writer_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(writer_url)
    await _create_tables(engine)

    async with engine.begin() as conn:
        # Minimal drawdown_state and cash_ledger (dashboard dependencies).
        await conn.execute(
            text("""
                INSERT INTO drawdown_state
                    (id, equity_high_water_mark_usd, current_drawdown_pct,
                     drawdown_duration_hours, lifetime_max_drawdown_pct,
                     drawdown_by_source_json, last_updated_at)
                VALUES ('current', 100000.0, 0.0, 0.0, 0.0, '{}',
                        '2026-05-26T00:00:00Z')
            """)
        )
        await conn.execute(
            text("""
                INSERT INTO cash_ledger
                    (id, current_cash_usd, settled_cash_usd, reserved_capital_usd,
                     available_buying_power_usd, margin_held_usd,
                     unsettled_proceeds_json, last_updated_at)
                VALUES ('current', '50000.00', '50000.00', '0.00',
                        '50000.00', '0.00', '[]', '2026-05-26T00:00:00Z')
            """)
        )

        # Two positions.
        pos_details_active = json.dumps(
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "unrealized_pnl_usd": "1000.00",
            }
        )
        pos_details_resolved = json.dumps(
            {
                "ticker": "TSLA",
                "sector": "Consumer Discretionary",
                "unrealized_pnl_usd": "-500.00",
            }
        )
        await conn.execute(
            text(
                "INSERT INTO positions"
                "    (position_id, status, instrument_type, direction, details_json,"
                "     execution_history_json, corporate_action_adjustment_needed)"
                " VALUES"
                "    ('pos-active', 'OPEN', 'EQUITY', 'LONG', :details, '[]', 0)"
            ),
            {"details": pos_details_active},
        )
        await conn.execute(
            text(
                "INSERT INTO positions"
                "    (position_id, status, instrument_type, direction, details_json,"
                "     execution_history_json, corporate_action_adjustment_needed)"
                " VALUES"
                "    ('pos-resolved', 'CLOSED', 'EQUITY', 'LONG', :details, '[]', 0)"
            ),
            {"details": pos_details_resolved},
        )

        # Active thesis with thesis_status ON_TRACK stored in narrative_json.
        await conn.execute(
            text("""
                INSERT INTO theses
                    (thesis_id, position_id, status, summary,
                     generation_timestamp, narrative_json)
                VALUES
                    ('ths-active', 'pos-active', 'ACTIVE',
                     'Bullish AAPL thesis',
                     '2026-05-24T10:00:00Z',
                     :narrative)
            """),
            {"narrative": json.dumps({"thesis_status": "ON_TRACK"})},
        )

        # Resolved thesis with resolution_category.
        await conn.execute(
            text("""
                INSERT INTO theses
                    (thesis_id, position_id, status, summary,
                     resolution_category, generation_timestamp,
                     resolution_timestamp, narrative_json)
                VALUES
                    ('ths-resolved', 'pos-resolved', 'RESOLVED',
                     'Bearish TSLA thesis',
                     'VALIDATED', '2026-05-20T10:00:00Z',
                     '2026-05-25T10:00:00Z',
                     :narrative)
            """),
            {"narrative": json.dumps({"thesis_status": "INVALIDATED"})},
        )

        # Two components for the active thesis.
        await conn.execute(
            text("""
                INSERT INTO thesis_components
                    (component_id, thesis_id, component_type, narrative,
                     key_assumptions_json, supporting_signals_json)
                VALUES
                    ('comp-entry', 'ths-active', 'ENTRY_RATIONALE',
                     'Strong earnings growth drives entry',
                     :assumptions, '[]')
            """),
            {"assumptions": json.dumps(["AI capex will exceed consensus", "margins expand"])},
        )
        await conn.execute(
            text("""
                INSERT INTO thesis_components
                    (component_id, thesis_id, component_type, narrative,
                     key_assumptions_json, supporting_signals_json)
                VALUES
                    ('comp-target', 'ths-active', 'TARGET_RATIONALE',
                     'Price target at $220 on multiple expansion',
                     '[]', '[]')
            """)
        )

        # One component for the resolved thesis with resolution_outcome.
        await conn.execute(
            text("""
                INSERT INTO thesis_components
                    (component_id, thesis_id, component_type, narrative,
                     key_assumptions_json, supporting_signals_json,
                     resolution_outcome, resolution_notes)
                VALUES
                    ('comp-tsla-entry', 'ths-resolved', 'ENTRY_RATIONALE',
                     'EV demand slowdown',
                     '[]', '[]',
                     'VALIDATED', 'Thesis played out correctly')
            """)
        )

        # Seed process_lifetimes parent row (required FK for invocations).
        await conn.execute(
            text(
                "INSERT INTO process_lifetimes "
                "(process_lifetime_id, process_role, process_start_at, process_pid, "
                " hostname, git_sha, git_branch, git_dirty, python_version, "
                " pip_freeze_hash, pip_freeze_snapshot_path, anthropic_sdk_version, "
                " claude_agent_sdk_version, os_release) "
                "VALUES "
                "('pl-1', 'pipeline', '2026-05-24T00:00:00Z', 1, 'host', "
                " :sha, 'main', 0, '3.13.1', :h, '/tmp/pip.txt', '0.40.0', "
                " '0.1.69', 'Darwin')"
            ),
            {"sha": "a" * 40, "h": "0" * 64},
        )

        # Seed invocation rows (required FK for activity_log).
        _inv_cols = (
            "invocation_id, process_lifetime_id, start_at, trigger_type,"
            " trigger_source, trigger_reason, git_sha_at_invocation,"
            " active_profile, active_regime, active_mode, active_overlays_json,"
            " resolved_config_hash, resolved_config_snapshot_path,"
            " feature_flags_snapshot_json, data_calibration_state_snapshot_path,"
            " data_source_freshness_json"
        )
        _inv_vals = (
            "(:inv_id, 'pl-1', :start, 'manual', 'cli', 'test',"
            " :sha, 'medium', 'normal', 'normal', '[]',"
            " :hash, '/tmp/cfg.json', '{}', '/tmp/cal.json', '{}')"
        )
        await conn.execute(
            text(f"INSERT INTO invocations ({_inv_cols}) VALUES {_inv_vals}"),
            {
                "inv_id": "inv-1",
                "start": "2026-05-24T12:00:00Z",
                "sha": "a" * 40,
                "hash": "0" * 64,
            },
        )
        await conn.execute(
            text(f"INSERT INTO invocations ({_inv_cols}) VALUES {_inv_vals}"),
            {
                "inv_id": "inv-2",
                "start": "2026-05-25T08:00:00Z",
                "sha": "a" * 40,
                "hash": "0" * 64,
            },
        )

        # Activity-log status-change events for the active thesis.
        # First transition: ACTIVE → ON_TRACK (no cited refs).
        await conn.execute(
            text("""
                INSERT INTO activity_log
                    (entry_id, invocation_id, entry_at, event_type, event_group,
                     thesis_id, source, detail_json)
                VALUES
                    ('log-1', 'inv-1', '2026-05-24T12:30:00Z',
                     'THESIS_STATUS_CHANGED', 'THESIS',
                     'ths-active', 'COMMAND_EXECUTOR',
                     :detail)
            """),
            {"detail": json.dumps({"old_status": "ACTIVE", "new_status": "ON_TRACK"})},
        )
        # Second transition: ON_TRACK → AT_RISK with a cited reference ID.
        await conn.execute(
            text("""
                INSERT INTO activity_log
                    (entry_id, invocation_id, entry_at, event_type, event_group,
                     thesis_id, source, detail_json)
                VALUES
                    ('log-2', 'inv-2', '2026-05-25T08:30:00Z',
                     'THESIS_STATUS_CHANGED', 'THESIS',
                     'ths-active', 'COMMAND_EXECUTOR',
                     :detail)
            """),
            {
                "detail": json.dumps(
                    {
                        "old_status": "ON_TRACK",
                        "new_status": "AT_RISK",
                        "cited_signal": "SA-7",
                    }
                )
            },
        )

    await engine.dispose()
    yield db_path


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"


"""Tests for GET /api/views/portfolio/theses (theses list)."""


class TestThesesListEndpoint:
    """GET /api/views/portfolio/theses — paginated filterable theses list."""

    def test_returns_200_with_theses(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses")
        assert resp.status_code == 200
        body = resp.json()
        assert "theses" in body
        assert "total" in body
        assert "page" in body
        assert "page_size" in body
        assert body["total"] == 2

    def test_active_thesis_row_fields(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses")
        body = resp.json()
        theses_by_id = {t["thesis_id"]: t for t in body["theses"]}
        active = theses_by_id["ths-active"]
        assert active["status"] == "ACTIVE"
        assert active["thesis_status"] == "ON_TRACK"
        assert active["position_id"] == "pos-active"
        assert active["summary"] == "Bullish AAPL thesis"
        assert active["age_hours"] > 0
        assert active["position_unrealized_pl_usd"] == "1000.00"

    def test_resolved_thesis_row_fields(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses")
        body = resp.json()
        theses_by_id = {t["thesis_id"]: t for t in body["theses"]}
        resolved = theses_by_id["ths-resolved"]
        assert resolved["status"] == "RESOLVED"
        assert resolved["resolution_category"] == "VALIDATED"
        assert resolved["resolution_timestamp"] is not None

    def test_filter_by_status_active(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?status=ACTIVE")
        body = resp.json()
        assert body["total"] == 1
        assert body["theses"][0]["thesis_id"] == "ths-active"

    def test_filter_by_status_resolved(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?status=RESOLVED")
        body = resp.json()
        assert body["total"] == 1
        assert body["theses"][0]["thesis_id"] == "ths-resolved"

    def test_filter_by_resolution_category(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?resolution_category=VALIDATED")
        body = resp.json()
        assert body["total"] == 1
        assert body["theses"][0]["thesis_id"] == "ths-resolved"

    def test_filter_by_classification(self, seeded_db: str) -> None:
        """Filter by thesis_status classification stored in narrative_json."""
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?classification=ON_TRACK")
        body = resp.json()
        assert body["total"] == 1
        assert body["theses"][0]["thesis_id"] == "ths-active"

    def test_filter_by_sector(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?sector=Technology")
        body = resp.json()
        assert body["total"] == 1
        assert body["theses"][0]["thesis_id"] == "ths-active"

    def test_filter_by_age_days_gt(self, seeded_db: str) -> None:
        """age_days_gt=1 returns theses older than 1 day from now."""
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?age_days_gt=1")
        body = resp.json()
        # Both seeded theses are > 1 day old (seeded 2026-05-20 and 2026-05-24).
        assert body["total"] == 2

    def test_pagination_page_size(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?page=1&page_size=1")
        body = resp.json()
        assert body["total"] == 2
        assert body["page_size"] == 1
        assert len(body["theses"]) == 1

    def test_empty_result_on_no_match(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses?status=CANCELLED")
        body = resp.json()
        assert body["total"] == 0
        assert body["theses"] == []


"""Tests for GET /api/views/portfolio/theses/{thesis_id} (thesis detail)."""


class TestThesisDetailEndpoint:
    """GET /api/views/portfolio/theses/{thesis_id} — full thesis detail."""

    def test_returns_200_for_active_thesis(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-active")
        assert resp.status_code == 200
        body = resp.json()
        assert body["thesis_id"] == "ths-active"
        assert body["status"] == "ACTIVE"
        assert body["thesis_status"] == "ON_TRACK"
        assert body["position_id"] == "pos-active"
        assert body["summary"] == "Bullish AAPL thesis"

    def test_returns_404_for_missing_thesis(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/does-not-exist")
        assert resp.status_code == 404

    def test_components_returned(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-active")
        body = resp.json()
        components = body["components"]
        assert len(components) == 2
        comp_types = {c["component_type"] for c in components}
        assert "ENTRY_RATIONALE" in comp_types
        assert "TARGET_RATIONALE" in comp_types

    def test_entry_component_key_assumptions(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-active")
        body = resp.json()
        entry_comp = next(c for c in body["components"] if c["component_type"] == "ENTRY_RATIONALE")
        assert "AI capex will exceed consensus" in entry_comp["key_assumptions"]
        assert "margins expand" in entry_comp["key_assumptions"]

    def test_status_history_timeline(self, seeded_db: str) -> None:
        """Status history should contain the two THESIS_STATUS_CHANGED events."""
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-active")
        body = resp.json()
        history = body["status_history"]
        assert len(history) == 2
        # Ordered by entry_at ascending.
        first, second = history[0], history[1]
        assert first["old_status"] == "ACTIVE"
        assert first["new_status"] == "ON_TRACK"
        assert second["old_status"] == "ON_TRACK"
        assert second["new_status"] == "AT_RISK"

    def test_cited_reference_ids_extracted(self, seeded_db: str) -> None:
        """Second status transition cites SA-7 in detail_json."""
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-active")
        body = resp.json()
        second_transition = body["status_history"][1]
        assert "SA-7" in second_transition["cited_reference_ids"]

    def test_no_cited_refs_when_absent(self, seeded_db: str) -> None:
        """First status transition has no reference IDs."""
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-active")
        body = resp.json()
        first_transition = body["status_history"][0]
        assert first_transition["cited_reference_ids"] == []

    def test_resolution_outcome_for_resolved_thesis(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-resolved")
        body = resp.json()
        assert body["status"] == "RESOLVED"
        assert body["resolution_category"] == "VALIDATED"
        # Component-level resolution_outcome should be in resolution_component_outcomes.
        assert "comp-tsla-entry" in body["resolution_component_outcomes"]
        assert body["resolution_component_outcomes"]["comp-tsla-entry"] == "VALIDATED"

    def test_component_resolution_notes(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-resolved")
        body = resp.json()
        comp = body["components"][0]
        assert comp["resolution_outcome"] == "VALIDATED"
        assert comp["resolution_notes"] == "Thesis played out correctly"

    def test_no_status_history_for_resolved_thesis(self, seeded_db: str) -> None:
        """Resolved thesis has no THESIS_STATUS_CHANGED events seeded."""
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-resolved")
        body = resp.json()
        assert body["status_history"] == []

    def test_age_hours_computed(self, seeded_db: str) -> None:
        reader = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader)
        with TestClient(app) as client:
            resp = client.get("/api/views/portfolio/theses/ths-active")
        body = resp.json()
        # Thesis generated 2026-05-24; should be > 24h old relative to 2026-05-26.
        assert body["age_hours"] > 24
