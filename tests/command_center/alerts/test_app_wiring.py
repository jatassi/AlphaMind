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


def test_alert_engine_factory_waits_for_lifespan(
    configs: tuple[Path, Path],
) -> None:
    """Regression for finding #3 (Wave-5 review).

    The supervisor registers the alerts factory before the FastAPI
    lifespan fires, so the engine isn't yet present when the factory
    first reads ``app.state``. The previous implementation returned a
    clean "no engine wired" with an info log; the supervisor's
    TaskGroup-as-fail-fast contract then cancelled everything, with no
    error visible to the operator. The fix polls until the engine
    appears (or the budget elapses) — exercise the polling path by
    invoking the factory while ``app.state`` is bare, then attaching
    the engine mid-poll and asserting the factory dispatches to
    ``engine.run``.
    """
    import asyncio
    from datetime import UTC, datetime

    from alphamind.command_center.session import ProcessSession

    config_dir, _ = configs
    # Build the app but do NOT enter the TestClient context — the lifespan
    # hasn't run so ``alert_engine`` is missing from ``app.state``.
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        alerts_overrides=AlertsOverrides(discord_channel=FakeDiscordChannel()),
    )
    # Confirm the prerequisite: the engine slot is not yet populated.
    assert not hasattr(app.state, "alert_engine")

    factory = app.state.alert_engine_task_factory
    session = ProcessSession(
        process_lifetime_id="plt-test",
        started_at=datetime.now(UTC),
    )

    ran = asyncio.Event()

    class _FakeEngine:
        async def run(self) -> None:
            ran.set()

    async def _drive() -> None:
        factory_task = asyncio.create_task(factory(session))
        # Wait one poll interval so the factory is in its polling loop,
        # then publish the engine. The factory must dispatch to
        # _FakeEngine.run rather than returning early.
        await asyncio.sleep(0.2)
        app.state.alert_engine = _FakeEngine()
        await asyncio.wait_for(ran.wait(), timeout=2.0)
        factory_task.cancel()
        # The cancellation propagates as CancelledError out of engine.run
        # — _FakeEngine.run already returned, so the factory completed
        # cleanly and we don't need to await further.

    asyncio.run(_drive())


def test_alert_engine_factory_raises_after_budget(
    configs: tuple[Path, Path],
) -> None:
    """If the lifespan never wires the engine, the factory fails loud.

    The previous early-return-on-missing-engine masked a startup-
    ordering bug as "alerts intentionally disabled"; the fix raises so
    the supervisor's TaskGroup propagates the error to the operator
    with a traceback.
    """
    import asyncio
    from datetime import UTC, datetime

    from alphamind.command_center import app as app_module
    from alphamind.command_center.session import ProcessSession

    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        alerts_overrides=AlertsOverrides(discord_channel=FakeDiscordChannel()),
    )
    factory = app.state.alert_engine_task_factory
    session = ProcessSession(
        process_lifetime_id="plt-test",
        started_at=datetime.now(UTC),
    )

    # Override the budget so the test doesn't sleep 30s.
    original_budget = app_module._ALERTS_ENGINE_WIRE_BUDGET_SECONDS
    app_module._ALERTS_ENGINE_WIRE_BUDGET_SECONDS = 0.3
    try:
        with pytest.raises(RuntimeError, match="alert engine was never wired"):
            asyncio.run(factory(session))
    finally:
        app_module._ALERTS_ENGINE_WIRE_BUDGET_SECONDS = original_budget


def test_alerts_data_dir_survives_hot_reload(
    tmp_path: Path,
    configs: tuple[Path, Path],
) -> None:
    """Wave-6 finding #3 — hot-reload must keep the disk-pressure rule live.

    Pre-fix ``_wire_alert_engine`` read ``getattr(app.state,
    'alerts_data_dir', None)`` for the rules-builder closure, but
    ``build_app`` never wrote that attribute. The closure returned
    ``data_dir=None`` on every hot-reload, swapping the live
    ``DataDirectoryDiskPressureCondition`` for a dormant placeholder —
    monitoring would silently stop after the first ``alerts.yaml`` edit.

    The fix mirrors ``alerts.data_dir`` onto ``app.state.alerts_data_dir``
    in build_app; the closure then re-renders the same live condition.
    """
    from alphamind.command_center.alerts.conditions import (
        DataDirectoryDiskPressureCondition,
    )

    config_dir, _ = configs
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
        config_dir=config_dir,
        alerts_overrides=AlertsOverrides(
            discord_channel=FakeDiscordChannel(),
            data_dir=data_dir,
        ),
    )
    # The state attr must be present even before the lifespan runs.
    assert app.state.alerts_data_dir == data_dir
    with TestClient(app):
        engine = app.state.alert_engine
        assert engine is not None
        # Construction path: the initial disk-pressure rule must be live
        # (not dormant) because alerts.data_dir was threaded through.
        disk_rule = next(r for r in engine.rules if str(r.name) == "data_directory_disk_pressure")
        assert isinstance(disk_rule.condition, DataDirectoryDiskPressureCondition)
        # Hot-reload path: the rules_builder closure captured inside
        # ``_wire_alert_engine`` must still produce a live condition (not
        # the dormant placeholder) when re-invoked. The builder is
        # private to the engine; ``_rules_builder`` is the attribute the
        # constructor stashes.
        rebuilt = engine._rules_builder(app.state.alerts_config)
        rebuilt_disk = next(r for r in rebuilt if str(r.name) == "data_directory_disk_pressure")
        assert isinstance(rebuilt_disk.condition, DataDirectoryDiskPressureCondition)


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
