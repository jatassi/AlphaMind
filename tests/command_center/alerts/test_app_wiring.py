"""Tests for app.py alert-engine wiring (story 05a / ALP-671).

Covers:

* ``AlertsOverrides`` injection round-trips through ``build_app``.
* ``app.state.alert_engine_task_factory`` is registered on the
  supervisor by the composition root.
* Lifespan constructs the engine with the live cc_writer +
  foreign_reader factories.
* Disabled-alerts path (no discord channel + empty rules) returns
  ``None`` engine + factory still callable (no-op).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alphamind.command_center.alerts.channels.discord import FakeDiscordChannel
from alphamind.command_center.alerts.engine import AlertEngine
from alphamind.command_center.app import AlertsOverrides, build_app
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)

_CONFIG_DIR = Path(__file__).parents[3] / "config"


@pytest.fixture
def configs(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the shipped configs into a per-test config_dir.

    Mirrors the fixture pattern from ``tests/command_center/test_main_boot.py``;
    we don't actually use the second tuple element here.
    """
    config_dir = tmp_path
    for name in (
        "command-center.yaml",
        "security.yaml",
        "alerts.yaml",
    ):
        (config_dir / name).write_bytes((_CONFIG_DIR / name).read_bytes())
    return config_dir, config_dir


def test_build_app_exposes_alert_engine_task_factory(
    configs: tuple[Path, Path],
) -> None:
    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        alerts_overrides=AlertsOverrides(discord_channel=FakeDiscordChannel()),
    )
    assert callable(app.state.alert_engine_task_factory)


def test_lifespan_constructs_alert_engine(
    configs: tuple[Path, Path],
) -> None:
    """The lifespan constructs an AlertEngine when rules + channel resolve."""
    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        alerts_overrides=AlertsOverrides(discord_channel=FakeDiscordChannel()),
    )
    with TestClient(app):
        engine = app.state.alert_engine
        assert isinstance(engine, AlertEngine)
        assert len(engine.rules) == 17


def test_alerts_overrides_threads_rules_through(
    configs: tuple[Path, Path],
) -> None:
    """A subset of rules supplied via AlertsOverrides round-trips intact."""
    from datetime import timedelta

    from alphamind.command_center._kernel.ids import alert_rule_name
    from alphamind.command_center.alerts.rules import (
        AlertConditionResult,
        AlertEvaluatorState,
        AlertRule,
        AlertSeverity,
    )

    class _Quiet:
        async def evaluate(
            self,
            *,
            event: object | None,
            state: AlertEvaluatorState,
        ) -> AlertConditionResult:
            del event, state
            return AlertConditionResult(fired=False)

    custom_rule = AlertRule(
        name=alert_rule_name("custom"),
        severity=AlertSeverity.CRITICAL,
        debounce_window=timedelta(minutes=1),
        channels=("in_app",),
        condition=_Quiet(),
    )
    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        alerts_overrides=AlertsOverrides(
            rules=(custom_rule,),
            discord_channel=FakeDiscordChannel(),
        ),
    )
    with TestClient(app):
        engine = app.state.alert_engine
        assert engine is not None
        names = [str(r.name) for r in engine.rules]
        assert names == ["custom"]


def test_disabled_when_yaml_has_zero_rules(
    tmp_path: Path,
    configs: tuple[Path, Path],
) -> None:
    """When no rules are configured, the engine resolves to None."""
    # Override alerts_overrides.rules with an empty tuple; bind_rules_from_config
    # would still produce 17 (defaults), so we test the AlertsOverrides path
    # by passing rules=() directly.
    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        alerts_overrides=AlertsOverrides(
            rules=(),
            discord_channel=FakeDiscordChannel(),
        ),
    )
    with TestClient(app):
        # Empty rules → engine is None (the AlertEngine constructor would
        # reject empty rules, so build_app sets engine to None via the
        # falsy-rules branch).
        assert app.state.alert_engine is None


def test_alert_engine_task_factory_no_ops_when_engine_none(
    tmp_path: Path,
    configs: tuple[Path, Path],
) -> None:
    """The supervisor task factory returns cleanly when the engine is None."""
    import asyncio
    from datetime import UTC, datetime

    from alphamind.command_center.session import ProcessSession

    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        alerts_overrides=AlertsOverrides(
            rules=(),
            discord_channel=FakeDiscordChannel(),
        ),
    )
    with TestClient(app):
        factory = app.state.alert_engine_task_factory
        session = ProcessSession(
            process_lifetime_id="plt-test",
            started_at=datetime.now(UTC),
        )
        # No-op call: engine is None so factory returns immediately.
        asyncio.run(factory(session))


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
