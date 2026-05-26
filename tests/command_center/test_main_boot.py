"""Boot smoke tests for ``python -m alphamind.command_center`` (story 02 / ALP-666).

The full ``main()`` entry path opens an engine pair against ``alphamind.db``,
records a ``process_lifetimes`` row, and starts Uvicorn — too much
machinery to exercise in a unit test. These tests exercise the in-process
boot of the FastAPI app under Uvicorn against an ephemeral port to
verify the AC's manual-smoke target:

* Uvicorn binds to a host/port specified at start.
* ``/healthz`` returns 200 over real HTTP (not just TestClient).
* The Uvicorn server task cancels cleanly when the supervisor
  ``request_stop()`` fires.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from alphamind.command_center.__main__ import _run_uvicorn_task
from alphamind.command_center.app import build_app
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.command_center.session import ProcessSession
from alphamind.command_center.supervisor import CommandCenterSupervisor


def _pick_free_port() -> int:
    """Bind a temporary socket to pick a free localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def per_test_config_dir(tmp_path: Path) -> Path:
    """Build a per-test config dir referencing a per-test DB.

    The lifespan opens engines against ``db.alphamind_db_path``; the
    SQLite file must exist on disk for the writer factory to bind
    against it (the writer factory's pragma hook fires on first connect
    which fails if the file is missing on Windows).
    """
    repo_config = Path(__file__).parents[2] / "config"
    db = tmp_path / "test.db"
    db.touch()
    cc_yaml = tmp_path / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: "{db}"
frontend:
  dist_path: "src/alphamind/command_center/frontend/dist"
pipeline:
  events_url: "http://127.0.0.1:8765"
monitor:
  events_url: "http://127.0.0.1:8766"
""",
        encoding="utf-8",
    )
    (tmp_path / "security.yaml").write_bytes((repo_config / "security.yaml").read_bytes())
    (tmp_path / "alerts.yaml").write_bytes((repo_config / "alerts.yaml").read_bytes())
    return tmp_path


class TestUvicornBoot:
    async def test_healthz_responds_200_over_real_http(
        self,
        per_test_config_dir: Path,
    ) -> None:
        port = _pick_free_port()
        app = build_app(
            command_center_config=load_command_center_config(per_test_config_dir),
            security_config=load_security_config(per_test_config_dir),
            alerts_config=load_alerts_config(per_test_config_dir),
        )

        session = ProcessSession(
            process_lifetime_id="plt-test",
            started_at=datetime.now(UTC),
        )
        supervisor = CommandCenterSupervisor(
            session=session,
            shutdown_timeout_seconds=5,
        )

        async def uvicorn_task(s: ProcessSession) -> None:
            await _run_uvicorn_task(s, app=app, host="127.0.0.1", port=port)

        supervisor.register_task(name="uvicorn", coro_fn=uvicorn_task)

        run_task = asyncio.create_task(supervisor.run())
        try:
            # Wait for the server to actually accept connections.
            url = f"http://127.0.0.1:{port}/healthz"
            for _attempt in range(40):
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(url, timeout=1.0)
                    break
                except (httpx.ConnectError, httpx.ReadError):
                    await asyncio.sleep(0.1)
            else:
                pytest.fail(f"could not reach {url} after retries")

            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
        finally:
            supervisor.request_stop()
            await asyncio.wait_for(run_task, timeout=10)

    async def test_supervisor_shutdown_cancels_uvicorn(
        self,
        per_test_config_dir: Path,
    ) -> None:
        port = _pick_free_port()
        app = build_app(
            command_center_config=load_command_center_config(per_test_config_dir),
            security_config=load_security_config(per_test_config_dir),
            alerts_config=load_alerts_config(per_test_config_dir),
        )

        session = ProcessSession(
            process_lifetime_id="plt-test",
            started_at=datetime.now(UTC),
        )
        supervisor = CommandCenterSupervisor(
            session=session,
            shutdown_timeout_seconds=5,
        )

        async def uvicorn_task(s: ProcessSession) -> None:
            await _run_uvicorn_task(s, app=app, host="127.0.0.1", port=port)

        supervisor.register_task(name="uvicorn", coro_fn=uvicorn_task)

        run_task = asyncio.create_task(supervisor.run())
        # Give Uvicorn a moment to bind.
        await asyncio.sleep(0.3)
        supervisor.request_stop()
        # The run_task must complete within the supervisor's shutdown
        # timeout window; if Uvicorn ignored the cancel, this would hang.
        await asyncio.wait_for(run_task, timeout=10)
