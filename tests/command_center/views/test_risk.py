"""Tests for ``command_center.views.risk`` (ALP-680).

Scoped pytest: ``uv run pytest tests/command_center/views/test_risk.py -n auto``.

Uses an on-disk tmp_path SQLite (not :memory:) so the foreign-reader factory
works (SQLite rejects ``?mode=ro`` on :memory: DBs).  The fixture seeds the
production-schema tables via ``alphamind.persistence.models.Base`` and reads
back through the foreign_reader session so endpoint logic is exercised
end-to-end.
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
from alphamind.command_center.views.risk import (
    WARMUP_DURATION_ESTIMATE,
    _build_exposure_rules,
    _classify_drawdown_tier,
    _classify_zone,
    _extract_market_value,
    build_risk_router,
)
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
    app.include_router(build_risk_router(), prefix="/api/views/risk")
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Fresh on-disk SQLite file with the production schema."""
    db_file = tmp_path / "test_risk.db"
    db_file.touch()
    return str(db_file)


@pytest.fixture
async def seeded_db(db_path: str) -> AsyncIterator[str]:
    """Create schema + seed minimal rows; yield db_path."""
    writer_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(writer_url)
    await _create_tables(engine)

    async with engine.begin() as conn:
        # Seed process_lifetimes (FK parent for invocations).
        await conn.execute(
            text("""
                INSERT INTO process_lifetimes
                    (process_lifetime_id, process_role, process_start_at,
                     process_pid, hostname, git_sha, git_branch, git_dirty,
                     python_version, pip_freeze_hash, pip_freeze_snapshot_path,
                     anthropic_sdk_version, claude_agent_sdk_version, os_release)
                VALUES
                    ('pl-1', 'pipeline', '2026-05-26T00:00:00Z',
                     12345, 'test-host', 'abc123', 'main', 0,
                     '3.13.0', 'hash0', '/pip.txt',
                     '0.1.0', '0.1.0', 'linux')
            """)
        )

        # Seed drawdown_state singleton.
        await conn.execute(
            text("""
                INSERT INTO drawdown_state
                    (id, equity_high_water_mark_usd, current_drawdown_pct,
                     drawdown_duration_hours, lifetime_max_drawdown_pct,
                     drawdown_by_source_json, last_updated_at)
                VALUES
                    ('current', 100000.0, 3.0, 12.0, 5.0, '{}',
                     '2026-05-26T00:00:00Z')
            """)
        )

        # Seed monitor_halt_mode singleton.
        await conn.execute(
            text("""
                INSERT INTO monitor_halt_mode (id, enabled, reason, applied_at)
                VALUES ('current', 0, NULL, NULL)
            """)
        )

        # Seed invocations row for calibration-mix tests.
        await conn.execute(
            text("""
                INSERT INTO invocations
                    (invocation_id, process_lifetime_id, start_at,
                     phase1_completed_at, phase2_completed_at,
                     trigger_type, trigger_source, trigger_reason,
                     git_sha_at_invocation, active_profile, active_regime,
                     active_mode, active_overlays_json, resolved_config_hash,
                     resolved_config_snapshot_path, feature_flags_snapshot_json,
                     data_calibration_state_snapshot_path, data_source_freshness_json)
                VALUES
                    ('inv-1', 'pl-1', '2026-05-26T10:00:00Z',
                     '2026-05-26T10:05:00Z', '2026-05-26T10:10:00Z',
                     'scheduled', 'pre_open', 'morning run',
                     'abc123', 'default', 'normal',
                     'normal', '[]', 'hash1',
                     '/data/config.json', '{}',
                     '/data/provenance/invocations/inv-1/data_calibration_state.json',
                     '{}')
            """)
        )

        # Seed activity_log: one guardrail rejection within last 24 h.
        await conn.execute(
            text("""
                INSERT INTO activity_log
                    (entry_id, invocation_id, entry_at, event_type, event_group,
                     position_id, order_id, thesis_id, source, detail_json)
                VALUES
                    ('entry-1', 'inv-1', '2026-05-26T12:00:00Z',
                     'GUARDRAIL_REJECTION', 'RISK_AND_GUARDRAIL',
                     NULL, NULL, NULL, 'GUARDRAIL_LAYER',
                     '{"rule": "net_long_exposure", "action": "BLOCK"}')
            """)
        )

        # Seed a RISK_PARAMETER_CHANGED event (older — outside recent 24 h window).
        await conn.execute(
            text("""
                INSERT INTO activity_log
                    (entry_id, invocation_id, entry_at, event_type, event_group,
                     position_id, order_id, thesis_id, source, detail_json)
                VALUES
                    ('entry-2', 'inv-1', '2020-01-01T00:00:00Z',
                     'RISK_PARAMETER_CHANGED', 'RISK_AND_GUARDRAIL',
                     NULL, NULL, NULL, 'GUARDRAIL_LAYER',
                     '{"rule": "cumulative_drawdown", "old": 8.0, "new": 10.0}')
            """)
        )

    await engine.dispose()
    yield db_path


@pytest.fixture
def client(seeded_db: str) -> TestClient:
    reader_factory = build_foreign_reader_session_factory(seeded_db)
    app = _app_with_reader(reader_factory)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Zone classification unit tests
# ---------------------------------------------------------------------------


class TestClassifyZone:
    def test_normal_zone(self) -> None:
        assert _classify_zone(0.0) == "normal"
        assert _classify_zone(69.9) == "normal"

    def test_warning_zone(self) -> None:
        assert _classify_zone(70.0) == "warning"
        assert _classify_zone(84.9) == "warning"

    def test_critical_zone(self) -> None:
        assert _classify_zone(85.0) == "critical"
        assert _classify_zone(94.9) == "critical"

    def test_hard_block_zone(self) -> None:
        assert _classify_zone(95.0) == "hard_block"
        assert _classify_zone(120.0) == "hard_block"

    def test_daily_drawdown_zone_overrides(self) -> None:
        """Daily drawdown has tighter thresholds: 60/80/90."""
        kw = {"warning_pct": 60.0, "critical_pct": 80.0, "hard_block_pct": 90.0}
        assert _classify_zone(59.9, **kw) == "normal"
        assert _classify_zone(60.0, **kw) == "warning"
        assert _classify_zone(80.0, **kw) == "critical"
        assert _classify_zone(90.0, **kw) == "hard_block"

    def test_cumulative_drawdown_zone_overrides(self) -> None:
        """Cumulative drawdown has tightest thresholds: 50/70/85."""
        kw = {"warning_pct": 50.0, "critical_pct": 70.0, "hard_block_pct": 85.0}
        assert _classify_zone(49.9, **kw) == "normal"
        assert _classify_zone(50.0, **kw) == "warning"
        assert _classify_zone(70.0, **kw) == "critical"
        assert _classify_zone(85.0, **kw) == "hard_block"


class TestClassifyDrawdownTier:
    def test_tier_0_normal(self) -> None:
        assert _classify_drawdown_tier(0.0) == 0
        assert _classify_drawdown_tier(7.9) == 0

    def test_tier_1_at_limit(self) -> None:
        assert _classify_drawdown_tier(8.0) == 1
        assert _classify_drawdown_tier(9.9) == 1

    def test_tier_2(self) -> None:
        assert _classify_drawdown_tier(10.0) == 2
        assert _classify_drawdown_tier(11.9) == 2

    def test_tier_3(self) -> None:
        assert _classify_drawdown_tier(12.0) == 3
        assert _classify_drawdown_tier(20.0) == 3


# ---------------------------------------------------------------------------
# _extract_market_value + _build_exposure_rules unit tests
# ---------------------------------------------------------------------------


class TestExtractMarketValue:
    def test_returns_zero_for_non_dict_json(self) -> None:
        """Regression for finding #15 (Wave-5 review).

        ``json.loads`` accepts arrays, scalars, and ``null`` — the
        downstream ``.get(...)`` raised :exc:`AttributeError` past the
        outer try/except that only caught :exc:`JSONDecodeError` /
        :exc:`TypeError` on the loads itself. The isinstance(dict) guard
        returns the miss sentinel (0.0) for any non-dict payload.
        """
        assert _extract_market_value("[1, 2, 3]") == 0.0
        assert _extract_market_value("42") == 0.0
        assert _extract_market_value("null") == 0.0
        assert _extract_market_value('"a string"') == 0.0

    def test_returns_value_for_dict_json(self) -> None:
        payload = json.dumps({"current_market_value_usd": -1234.5})
        assert _extract_market_value(payload) == -1234.5

    def test_falls_back_through_keys(self) -> None:
        payload = json.dumps({"entry_value_usd": 7.0})
        assert _extract_market_value(payload) == 7.0


class TestBuildExposureRules:
    """Regression for finding #6 (Wave-5 review).

    SHORT positions report a negative ``current_market_value_usd``
    (held-short equity); accumulating the signed value into the gross
    exposure caused a delta-neutral book to surface as 0 % gross
    exposure — silent under-reporting of the rollup the guardrail
    dashboard surfaces. The fix applies :func:`abs` to the
    per-position market value before summing.
    """

    @staticmethod
    def _position(direction: str, market_value: float) -> dict[str, Any]:
        return {
            "direction": direction,
            "details_json": json.dumps({"current_market_value_usd": market_value}),
        }

    def test_delta_neutral_book_reports_full_gross(self) -> None:
        positions = [
            self._position("LONG", 80_000.0),
            self._position("SHORT", -80_000.0),
        ]
        rules = {r.rule_name: r for r in _build_exposure_rules(positions, 100_000.0)}
        # gross_notional = 80k + 80k = 160k; pv = 100k → 160 %.
        assert rules["gross_exposure"].current_value == pytest.approx(160.0)
        # Headroom against the 120 % limit is fully consumed.
        assert rules["gross_exposure"].headroom_pct == 0.0
        # Net-long / net-short each surface as positive percentages.
        assert rules["net_long_exposure"].current_value == pytest.approx(80.0)
        assert rules["net_short_exposure"].current_value == pytest.approx(80.0)

    def test_short_positions_with_negative_market_value(self) -> None:
        positions = [
            self._position("SHORT", -50_000.0),
        ]
        rules = {r.rule_name: r for r in _build_exposure_rules(positions, 100_000.0)}
        # Without abs() this would surface as -50 % gross, which is nonsense.
        assert rules["gross_exposure"].current_value == pytest.approx(50.0)
        assert rules["net_short_exposure"].current_value == pytest.approx(50.0)

    def test_empty_positions_returns_empty_when_pv_zero(self) -> None:
        assert _build_exposure_rules([], 0.0) == []


# ---------------------------------------------------------------------------
# Guardrail dashboard endpoint tests
# ---------------------------------------------------------------------------


class TestGuardrailDashboard:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/views/risk/guardrail-dashboard")
        assert resp.status_code == 200

    def test_response_has_rules(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        assert "rules" in data
        assert isinstance(data["rules"], list)

    def test_rules_have_documented_fields(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        for rule in data["rules"]:
            assert "rule_name" in rule
            assert "current_value" in rule
            assert "limit_value" in rule
            assert "headroom_pct" in rule
            assert "zone" in rule
            assert rule["zone"] in {"normal", "warning", "critical", "hard_block"}

    def test_contains_drawdown_rules(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        rule_names = [r["rule_name"] for r in data["rules"]]
        assert "daily_drawdown" in rule_names
        assert "cumulative_drawdown" in rule_names

    def test_contains_exposure_rules(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        rule_names = [r["rule_name"] for r in data["rules"]]
        assert "gross_exposure" in rule_names
        assert "net_long_exposure" in rule_names
        assert "net_short_exposure" in rule_names

    def test_drawdown_status_fields(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        dd = data["drawdown"]
        assert "current_drawdown_pct" in dd
        assert "equity_high_water_mark_usd" in dd
        assert "daily_drawdown_pct" in dd
        assert "daily_drawdown_limit_pct" in dd
        assert "daily_zone" in dd
        assert "cumulative_drawdown_pct" in dd
        assert "cumulative_drawdown_limit_pct" in dd
        assert "cumulative_zone" in dd
        assert "progressive_tier" in dd
        assert "halt_mode_engaged" in dd

    def test_drawdown_cumulative_zone_matches_seeded_value(self, client: TestClient) -> None:
        # seeded: current_drawdown_pct = 3.0 — below 8% limit → tier 0 / normal zone
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        dd = data["drawdown"]
        assert dd["current_drawdown_pct"] == pytest.approx(3.0, abs=0.01)
        assert dd["progressive_tier"] == 0
        # 3.0/8.0 = 37.5% consumed → well below 50% warning threshold for cumulative
        assert dd["cumulative_zone"] == "normal"

    def test_recent_breaches_contains_guardrail_rejection(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        assert "recent_breaches" in data
        # The seeded entry-1 has a future-ish timestamp so it is within 24h.
        # entry-2 is from 2020 so outside the window.
        breach_types = [b["event_type"] for b in data["recent_breaches"]]
        assert "GUARDRAIL_REJECTION" in breach_types
        # Old RISK_PARAMETER_CHANGED from 2020 should NOT appear.
        for b in data["recent_breaches"]:
            assert b["entry_at"] >= "2026"

    def test_active_multipliers_and_overlays_present(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/guardrail-dashboard").json()
        assert "active_multipliers_and_overlays" in data
        amo = data["active_multipliers_and_overlays"]
        assert "halt_mode_engaged" in amo
        assert "multipliers" in amo
        assert "active_overlays" in amo
        assert amo["halt_mode_engaged"] is False  # seeded 0

    def test_halt_mode_flag_when_engaged(self, seeded_db: str) -> None:
        """Engaging halt mode surfaces in both drawdown and active_multipliers."""
        import asyncio

        async def _engage_halt(path: str) -> None:
            writer_url = f"sqlite+aiosqlite:///{path}"
            eng = create_async_engine(writer_url)
            async with eng.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE monitor_halt_mode SET enabled=1, reason='test', "
                        "applied_at='2026-05-26T12:00:00Z' WHERE id='current'"
                    )
                )
            await eng.dispose()

        asyncio.get_event_loop().run_until_complete(_engage_halt(seeded_db))
        reader_factory = build_foreign_reader_session_factory(seeded_db)
        app = _app_with_reader(reader_factory)
        c = TestClient(app)
        data = c.get("/api/views/risk/guardrail-dashboard").json()
        assert data["drawdown"]["halt_mode_engaged"] is True
        assert data["active_multipliers_and_overlays"]["halt_mode_engaged"] is True


# ---------------------------------------------------------------------------
# Regime timeline endpoint tests
# ---------------------------------------------------------------------------


class TestRegimeTimeline:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/views/risk/regime-timeline")
        assert resp.status_code == 200

    def test_response_shape(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/regime-timeline").json()
        assert "events" in data
        assert "from_ts" in data
        assert "to_ts" in data
        assert isinstance(data["events"], list)

    def test_custom_time_window(self, client: TestClient) -> None:
        resp = client.get(
            "/api/views/risk/regime-timeline",
            params={"from": "2026-05-26T00:00:00Z", "to": "2026-05-27T00:00:00Z"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["from_ts"] == "2026-05-26T00:00:00Z"
        assert data["to_ts"] == "2026-05-27T00:00:00Z"

    def test_events_have_documented_fields(self, client: TestClient) -> None:
        # Seed a HALT_ACTIVATED event in the default window (last 24 h).
        # The existing entry-1 (GUARDRAIL_REJECTION) should appear if in timeline_types.
        data = client.get("/api/views/risk/regime-timeline").json()
        for evt in data["events"]:
            assert "entry_at" in evt
            assert "event_type" in evt
            assert "event_group" in evt
            assert "detail_json" in evt

    def test_guardrail_rejection_in_timeline(self, client: TestClient) -> None:
        """GUARDRAIL_REJECTION is in the timeline event types."""
        data = client.get(
            "/api/views/risk/regime-timeline",
            params={"from": "2026-05-01T00:00:00Z", "to": "2026-05-27T23:59:59Z"},
        ).json()
        event_types = {e["event_type"] for e in data["events"]}
        assert "GUARDRAIL_REJECTION" in event_types


# ---------------------------------------------------------------------------
# Calibration mix endpoint tests
# ---------------------------------------------------------------------------


class TestCalibrationMix:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/views/risk/calibration-mix")
        assert resp.status_code == 200

    def test_response_shape(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/calibration-mix").json()
        assert "per_invocation" in data
        assert "seven_day_trend" in data
        assert "stuck_blocks" in data
        assert "warmup_duration_estimate" in data

    def test_warmup_duration_estimate_present(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/calibration-mix").json()
        assert data["warmup_duration_estimate"] == WARMUP_DURATION_ESTIMATE

    def test_warmup_estimate_references_key_durations(self, client: TestClient) -> None:
        """The warm-up estimate cross-references key baselines from threshold-calibration.md."""
        data = client.get("/api/views/risk/calibration-mix").json()
        est = data["warmup_duration_estimate"]
        # Should mention volume/ATR, sentiment, gap-fill, extended-hours, lead-lag
        assert "20 trading days" in est or "Volume" in est
        assert "gap-fill" in est.lower() or "gap_fill" in est.lower()

    def test_per_invocation_returns_list(self, client: TestClient) -> None:
        data = client.get("/api/views/risk/calibration-mix").json()
        assert isinstance(data["per_invocation"], list)

    def test_custom_invocations_window(self, client: TestClient) -> None:
        resp = client.get("/api/views/risk/calibration-mix", params={"invocations_window": 5})
        assert resp.status_code == 200

    def test_invocations_window_bounds(self, client: TestClient) -> None:
        """invocations_window must be in [1, 100]."""
        url = "/api/views/risk/calibration-mix"
        assert client.get(url, params={"invocations_window": 0}).status_code == 422
        assert client.get(url, params={"invocations_window": 101}).status_code == 422

    def test_calibration_mix_with_file(self, seeded_db: str, tmp_path: Path) -> None:
        """When a data_calibration_state.json exists, per_invocation is populated."""
        # Create the provenance file the test invocation references.
        prov_dir = tmp_path / "provenance" / "invocations" / "inv-1"
        prov_dir.mkdir(parents=True)
        calib_state = {
            "schema_version": "2",
            "invocation_id": "inv-1",
            "as_of": "2026-05-26T10:10:00Z",
            "summary": {
                "total_blocks": 10,
                "by_state": {"calibrated": 7, "accumulating": 2, "unavailable": 1},
            },
            "accumulating_reasons": {"q3.options_flow": "insufficient observations"},
            "unavailable_reasons": {"q1.gap": "no resolved events yet"},
        }
        (prov_dir / "data_calibration_state.json").write_text(
            json.dumps(calib_state), encoding="utf-8"
        )

        reader_factory = build_foreign_reader_session_factory(seeded_db)

        class _MockApp(FastAPI):
            pass

        app = _MockApp()
        app.state.foreign_reader_session_factory = reader_factory
        # Point data_dir at tmp_path/provenance by patching the config attribute.
        # We don't construct a full config; instead we patch _resolve_data_dir
        # by injecting a fake config onto app.state with a db_path pointing into
        # the tmp_path.
        fake_db_path_str = str(tmp_path / "alphamind.db")

        class _FakeDbCfg:
            alphamind_db_path = fake_db_path_str

        class _FakeCcCfg:
            db = _FakeDbCfg()

        app.state.command_center_config = _FakeCcCfg()
        app.include_router(build_risk_router(), prefix="/api/views/risk")
        c = TestClient(app)

        data = c.get("/api/views/risk/calibration-mix").json()
        assert len(data["per_invocation"]) == 1
        inv = data["per_invocation"][0]
        assert inv["invocation_id"] == "inv-1"
        assert inv["total_blocks"] == 10
        assert inv["calibrated"] == 7
        assert inv["accumulating"] == 2
        assert inv["unavailable"] == 1
        assert inv["calibrated_pct"] == pytest.approx(70.0, abs=0.1)

    def test_stuck_blocks_identified(self, seeded_db: str, tmp_path: Path) -> None:
        """Blocks stuck in accumulating/unavailable surface in stuck_blocks."""
        prov_dir = tmp_path / "provenance" / "invocations" / "inv-1"
        prov_dir.mkdir(parents=True)
        calib_state = {
            "schema_version": "2",
            "invocation_id": "inv-1",
            "as_of": "2026-05-26T10:10:00Z",
            "summary": {
                "total_blocks": 5,
                "by_state": {"calibrated": 3, "accumulating": 1, "unavailable": 1},
            },
            "accumulating_reasons": {"q3.options_flow": "insufficient observations"},
            "unavailable_reasons": {"q1.gap": "no resolved events yet"},
        }
        (prov_dir / "data_calibration_state.json").write_text(
            json.dumps(calib_state), encoding="utf-8"
        )

        reader_factory = build_foreign_reader_session_factory(seeded_db)
        app = FastAPI()
        app.state.foreign_reader_session_factory = reader_factory

        class _FakeDbCfg:
            alphamind_db_path = str(tmp_path / "alphamind.db")

        class _FakeCcCfg:
            db = _FakeDbCfg()

        app.state.command_center_config = _FakeCcCfg()
        app.include_router(build_risk_router(), prefix="/api/views/risk")
        c = TestClient(app)

        data = c.get("/api/views/risk/calibration-mix").json()
        stuck_block_ids = {b["block_id"] for b in data["stuck_blocks"]}
        assert "q3.options_flow" in stuck_block_ids
        assert "q1.gap" in stuck_block_ids

    def test_calibration_bar_segments_match_counts(self, seeded_db: str, tmp_path: Path) -> None:
        """calibrated + accumulating + unavailable == total_blocks."""
        prov_dir = tmp_path / "provenance" / "invocations" / "inv-1"
        prov_dir.mkdir(parents=True)
        calib_state = {
            "schema_version": "2",
            "invocation_id": "inv-1",
            "as_of": "2026-05-26T10:10:00Z",
            "summary": {
                "total_blocks": 20,
                "by_state": {"calibrated": 15, "accumulating": 4, "unavailable": 1},
            },
            "accumulating_reasons": {},
            "unavailable_reasons": {},
        }
        (prov_dir / "data_calibration_state.json").write_text(
            json.dumps(calib_state), encoding="utf-8"
        )

        reader_factory = build_foreign_reader_session_factory(seeded_db)
        app = FastAPI()
        app.state.foreign_reader_session_factory = reader_factory

        class _FakeDbCfg:
            alphamind_db_path = str(tmp_path / "alphamind.db")

        class _FakeCcCfg:
            db = _FakeDbCfg()

        app.state.command_center_config = _FakeCcCfg()
        app.include_router(build_risk_router(), prefix="/api/views/risk")
        c = TestClient(app)

        data = c.get("/api/views/risk/calibration-mix").json()
        assert len(data["per_invocation"]) == 1
        inv = data["per_invocation"][0]
        assert inv["calibrated"] + inv["accumulating"] + inv["unavailable"] == inv["total_blocks"]
