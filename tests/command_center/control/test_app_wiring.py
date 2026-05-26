"""Tests for the control-router wiring in ``command_center.app`` (ALP-668).

Confirms ``build_app(control_overrides=...)`` injects fake clients
without booting the lifespan path that constructs the
:class:`RealPipelineClient` / :class:`RealMonitorClient`, and that the
production lifespan constructs the Real* clients when no overrides
are provided.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alphamind.command_center.app import (
    AuthOverrides,
    ControlOverrides,
    build_app,
)
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.command_center.control.monitor_client import (
    FakeMonitorClient,
    RealMonitorClient,
)
from alphamind.command_center.control.pipeline_client import (
    FakePipelineClient,
    RealPipelineClient,
)


@pytest.fixture
def configs(tmp_path: Path) -> Path:
    repo_config = Path(__file__).parents[3] / "config"
    db = tmp_path / "test.db"
    db.touch()
    (tmp_path / "command-center.yaml").write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: "{db}"
frontend:
  dist_path: "src/alphamind/command_center/frontend/dist"
pipeline:
  control_url: "http://127.0.0.1:8765"
monitor:
  control_url: "http://127.0.0.1:8766"
""",
        encoding="utf-8",
    )
    (tmp_path / "security.yaml").write_bytes((repo_config / "security.yaml").read_bytes())
    (tmp_path / "alerts.yaml").write_bytes((repo_config / "alerts.yaml").read_bytes())
    return tmp_path


@pytest.fixture
def client_with_overrides(configs: Path) -> Iterator[TestClient]:
    fake_pipeline = FakePipelineClient()
    fake_monitor = FakeMonitorClient()
    app = build_app(
        command_center_config=load_command_center_config(configs),
        security_config=load_security_config(configs),
        alerts_config=load_alerts_config(configs),
        process_lifetime_id="plt-wiring-test",
        auth_overrides=AuthOverrides(),
        control_overrides=ControlOverrides(
            pipeline_client=fake_pipeline,
            monitor_client=fake_monitor,
        ),
    )
    with TestClient(app) as client:
        yield client


class TestControlOverridesWire:
    def test_pipeline_client_on_app_state_is_fake_when_override_supplied(
        self, configs: Path
    ) -> None:
        fake = FakePipelineClient()
        app = build_app(
            command_center_config=load_command_center_config(configs),
            security_config=load_security_config(configs),
            alerts_config=load_alerts_config(configs),
            process_lifetime_id="plt-x",
            control_overrides=ControlOverrides(pipeline_client=fake),
        )
        with TestClient(app):
            assert app.state.pipeline_client is fake

    def test_monitor_client_on_app_state_is_fake_when_override_supplied(
        self, configs: Path
    ) -> None:
        fake = FakeMonitorClient()
        app = build_app(
            command_center_config=load_command_center_config(configs),
            security_config=load_security_config(configs),
            alerts_config=load_alerts_config(configs),
            process_lifetime_id="plt-x",
            control_overrides=ControlOverrides(monitor_client=fake),
        )
        with TestClient(app):
            assert app.state.monitor_client is fake


class TestLifespanConstructsRealClients:
    def test_real_pipeline_client_constructed_when_no_override(
        self, configs: Path
    ) -> None:
        app = build_app(
            command_center_config=load_command_center_config(configs),
            security_config=load_security_config(configs),
            alerts_config=load_alerts_config(configs),
            process_lifetime_id="plt-x",
        )
        with TestClient(app):
            assert isinstance(app.state.pipeline_client, RealPipelineClient)
            assert isinstance(app.state.monitor_client, RealMonitorClient)
            # Both clients share the lifespan-owned httpx client.
            assert app.state.control_http_client is not None

    def test_lifespan_closes_shared_httpx_client_on_shutdown(
        self, configs: Path
    ) -> None:
        app = build_app(
            command_center_config=load_command_center_config(configs),
            security_config=load_security_config(configs),
            alerts_config=load_alerts_config(configs),
            process_lifetime_id="plt-x",
        )
        with TestClient(app):
            http_client = app.state.control_http_client
            assert http_client is not None
            assert not http_client.is_closed
        # After the TestClient block exits, the lifespan teardown ran.
        assert http_client.is_closed


class TestProcessLifetimeIdOnAppState:
    def test_process_lifetime_id_threaded_through(self, configs: Path) -> None:
        app = build_app(
            command_center_config=load_command_center_config(configs),
            security_config=load_security_config(configs),
            alerts_config=load_alerts_config(configs),
            process_lifetime_id="plt-the-test-value",
        )
        assert app.state.process_lifetime_id == "plt-the-test-value"

    def test_process_lifetime_id_default_none(self, configs: Path) -> None:
        app = build_app(
            command_center_config=load_command_center_config(configs),
            security_config=load_security_config(configs),
            alerts_config=load_alerts_config(configs),
        )
        assert app.state.process_lifetime_id is None


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
