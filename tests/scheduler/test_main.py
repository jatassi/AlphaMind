"""Tests for the ``python -m alphamind.scheduler`` CLI entry point (stories 01 + 03b).

We exercise the CLI shim directly via ``main(argv)`` and assert on
behaviour at the parsing + dispatch layer. The actual ``run_invocation``
orchestrator is exercised by ``tests/scheduler/test_orchestrator.py``; the
CLI-side tests verify the ``--once`` branch routes to ``run_invocation``
with the parsed kwargs and emits the summary as JSON.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, cast

import pytest

from alphamind.config.models.run_types import RunType
from alphamind.scheduler.__main__ import main


def _make_summary_stub(invocation_id: str = "inv-stub-1") -> Any:
    """Return an ``InvocationSummary``-like object the CLI can serialize."""
    from alphamind.execution.write_paths.fill_collection import FillCollectionSummary
    from alphamind.scheduler.orchestrator import InvocationSummary

    return InvocationSummary(
        invocation_id=invocation_id,
        trigger_type="manual",
        trigger_source="cli",
        firing_run_type=RunType.market_hours_rolling,
        fill_collection_summary=FillCollectionSummary(
            fills_processed=0,
            fills_quarantined=0,
            ca_activities_processed=0,
            reconciliation_alerts=0,
        ),
        commands_submitted=0,
        commands_rejected=0,
        staleness_flag=False,
        duration_seconds=0.5,
    )


def _patch_cli_heavy_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the engine / process-lifetime / venue-config slots ``_run_once`` reads.

    The CLI tests verify the orchestrator-call kwargs end-to-end, but the
    process-lifetime row insertion runs ``pip freeze`` and the venue
    config reader hits the filesystem; both are infrastructure plumbing
    the orchestrator tests already cover. Stubbing here keeps the
    CLI-test scope narrow.
    """
    from alphamind.config.models.venue import (
        Alpaca,
        AlpacaCredentials,
        SessionHours,
        SessionWindow,
        VenueConfig,
    )
    from alphamind.scheduler import __main__ as module

    fake_venue = VenueConfig(
        alpaca=Alpaca(
            paper=AlpacaCredentials(
                rest_url="https://paper-api.alpaca.markets",
                ws_url="wss://paper-api.alpaca.markets",
                api_key_env="ALPACA_PAPER_KEY",
                api_secret_env="ALPACA_PAPER_SECRET",
            ),
            live=AlpacaCredentials(
                rest_url="https://api.alpaca.markets",
                ws_url="wss://api.alpaca.markets",
                api_key_env="ALPACA_LIVE_KEY",
                api_secret_env="ALPACA_LIVE_SECRET",
            ),
            rate_limit_per_minute=200,
        ),
        session_hours=SessionHours(
            regular=SessionWindow(open="09:30", close="16:00"),
            pre_market=SessionWindow(open="04:00", close="09:30"),
            after_hours=SessionWindow(open="16:00", close="20:00"),
        ),
    )
    monkeypatch.setattr(module, "_load_venue_config", lambda config_dir: fake_venue)

    async def _stub_process_lifetime(**_kwargs: Any) -> str:
        return "proc-cli-1"

    monkeypatch.setattr(module, "record_process_lifetime", _stub_process_lifetime)

    @contextlib.asynccontextmanager
    async def _stub_engine_pair(path: str | None = None) -> Any:
        from alphamind.persistence.session import EnginePair

        yield EnginePair(
            async_engine=cast(Any, object()),
            async_session_factory=cast(Any, object()),
            sync_engine=cast(Any, object()),
            sync_session_factory=cast(Any, object()),
        )

    monkeypatch.setattr(module, "engine_pair_context", _stub_engine_pair)
    monkeypatch.setattr(module, "configure_pipeline_logging", lambda: None)


class TestCliRunOnce:
    def test_run_once_invokes_run_invocation_and_prints_summary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--once`` routes to ``run_invocation`` and prints the summary JSON."""
        from alphamind.scheduler import __main__ as module

        _patch_cli_heavy_setup(monkeypatch)
        captured: dict[str, Any] = {}

        async def _stub(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return _make_summary_stub()

        monkeypatch.setattr(module, "run_invocation", _stub)

        main(
            argv=[
                "run",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test reason",
                "--mode",
                "paper",
            ]
        )

        assert captured["trigger_type"] == "manual"
        assert captured["trigger_source"] == "cli"
        assert captured["trigger_reason"] == "test reason"
        assert captured["firing_run_type"] is RunType.market_hours_rolling
        assert captured["context"].process_lifetime_id == "proc-cli-1"

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["invocation_id"] == "inv-stub-1"
        assert payload["firing_run_type"] == "market_hours_rolling"
        assert payload["commands_submitted"] == 0
        assert payload["commands_rejected"] == 0

    def test_run_once_rejects_unknown_run_type(self) -> None:
        """Bad ``<run_type>`` token still surfaces as ``SystemExit``."""
        with pytest.raises(SystemExit):
            main(
                argv=[
                    "run",
                    "--once",
                    "definitely_not_a_run_type",
                    "--reason",
                    "test",
                ]
            )

    def test_run_once_propagates_run_invocation_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exception raised by ``run_invocation`` propagates to the caller."""
        from alphamind.scheduler import __main__ as module

        _patch_cli_heavy_setup(monkeypatch)

        async def _raising_stub(**_kwargs: Any) -> Any:
            msg = "orchestrator failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(module, "run_invocation", _raising_stub)

        with pytest.raises(RuntimeError, match="orchestrator failed"):
            main(
                argv=[
                    "run",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                ]
            )


class TestCliArgparseSurface:
    def test_no_arguments_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(argv=[])

    def test_unknown_subcommand_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit):
            main(argv=["nope"])

    def test_run_once_requires_reason(self) -> None:
        with pytest.raises(SystemExit):
            main(argv=["run", "--once", "market_hours_rolling"])

    def test_fresh_start_in_daemon_mode_rejected(self) -> None:
        """``--fresh-start`` without ``--once`` (and without ``--debug-e2e``) is rejected.

        The production bootstrap is a one-shot operation by design; an
        operator running it under the daemon would silently skip the
        bootstrap on every subsequent invocation.
        """
        with pytest.raises(SystemExit):
            main(argv=["run", "--fresh-start", "--mode", "paper"])


class TestCliRunOnceFreshStart:
    """Production cold-start bootstrap path (ALP-620).

    With ``--fresh-start`` and without ``--debug-e2e``, ``_run_once`` must
    call :func:`run_fresh_start_bootstrap` before invoking
    :func:`run_invocation`; without ``--fresh-start`` the bootstrap must be
    skipped.
    """

    def test_invokes_bootstrap_before_run_invocation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alphamind.scheduler import __main__ as module

        _patch_cli_heavy_setup(monkeypatch)

        call_order: list[str] = []

        async def _stub_bootstrap(**_kwargs: Any) -> None:
            call_order.append("bootstrap")

        async def _stub_run_invocation(**_kwargs: Any) -> Any:
            call_order.append("run_invocation")
            return _make_summary_stub()

        monkeypatch.setattr(module, "run_fresh_start_bootstrap", _stub_bootstrap)
        monkeypatch.setattr(module, "run_invocation", _stub_run_invocation)

        main(
            argv=[
                "run",
                "--fresh-start",
                "--once",
                "market_open",
                "--reason",
                "first-run bootstrap",
                "--mode",
                "paper",
            ]
        )

        assert call_order == ["bootstrap", "run_invocation"]

    def test_skips_bootstrap_without_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alphamind.scheduler import __main__ as module

        _patch_cli_heavy_setup(monkeypatch)

        bootstrap_called: dict[str, bool] = {"called": False}

        async def _stub_bootstrap(**_kwargs: Any) -> None:
            bootstrap_called["called"] = True

        async def _stub_run_invocation(**_kwargs: Any) -> Any:
            return _make_summary_stub()

        monkeypatch.setattr(module, "run_fresh_start_bootstrap", _stub_bootstrap)
        monkeypatch.setattr(module, "run_invocation", _stub_run_invocation)

        main(
            argv=[
                "run",
                "--once",
                "market_open",
                "--reason",
                "no bootstrap",
            ]
        )

        assert bootstrap_called["called"] is False

    def test_bootstrap_failure_aborts_before_run_invocation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``FreshStartPreconditionError`` from bootstrap must abort the
        CLI before any invocation row is opened."""
        from alphamind.scheduler import __main__ as module
        from alphamind.scheduler.fresh_start import FreshStartPreconditionError

        _patch_cli_heavy_setup(monkeypatch)

        async def _failing_bootstrap(**_kwargs: Any) -> None:
            msg = "positions present"
            raise FreshStartPreconditionError(msg)

        run_invocation_called: dict[str, bool] = {"called": False}

        async def _stub_run_invocation(**_kwargs: Any) -> Any:
            run_invocation_called["called"] = True
            return _make_summary_stub()

        monkeypatch.setattr(module, "run_fresh_start_bootstrap", _failing_bootstrap)
        monkeypatch.setattr(module, "run_invocation", _stub_run_invocation)

        with pytest.raises(FreshStartPreconditionError, match="positions present"):
            main(
                argv=[
                    "run",
                    "--fresh-start",
                    "--once",
                    "market_open",
                    "--reason",
                    "blocked bootstrap",
                ]
            )
        assert run_invocation_called["called"] is False


class TestCliConfiguresUtf8Stdio:
    def test_main_calls_configure_utf8_stdio(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``main()`` must reconfigure stdio to UTF-8 before dispatching.

        Windows defaults stdout to cp1252; when ``verify_debug_e2e.py``
        captures the subprocess stdout (the InvocationSummary JSON),
        any non-ASCII codepoint surfaces as ``UnicodeEncodeError`` and
        loses the captured payload. The verify script already calls
        ``configure_utf8_stdio()`` itself; the subprocess needs the
        same treatment.
        """
        from alphamind.scheduler import __main__ as module

        _patch_cli_heavy_setup(monkeypatch)

        called: dict[str, bool] = {"called": False}

        def _stub_configure() -> None:
            called["called"] = True

        monkeypatch.setattr(module, "configure_utf8_stdio", _stub_configure)

        async def _stub(**_kwargs: Any) -> Any:
            return _make_summary_stub()

        monkeypatch.setattr(module, "run_invocation", _stub)

        main(
            argv=[
                "run",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )

        assert called["called"] is True


class TestCliDaemonRegistersEmergencyReceiver:
    def test_daemon_path_registers_emergency_receiver_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The daemon-mode branch must register a task named
        ``emergency_receiver`` on the ``PipelineSupervisor`` (story 04b)."""
        from alphamind.scheduler import __main__ as module
        from alphamind.scheduler.supervisor import PipelineSupervisor

        _patch_cli_heavy_setup(monkeypatch)

        # Capture the supervisor instance the daemon path constructs so we can
        # inspect its task registry once ``main()`` returns.
        captured: dict[str, Any] = {}

        original_supervisor_cls = PipelineSupervisor

        class _SpySupervisor(PipelineSupervisor):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                captured["supervisor"] = self

            async def run(self) -> None:
                # Skip the actual supervisor loop -- we only want to observe
                # that the task was registered before ``run()`` was awaited.
                return None

        monkeypatch.setattr(module, "PipelineSupervisor", _SpySupervisor)

        main(argv=["run", "--mode", "paper"])

        supervisor = captured["supervisor"]
        assert isinstance(supervisor, original_supervisor_cls)
        assert "emergency_receiver" in supervisor.task_names()
