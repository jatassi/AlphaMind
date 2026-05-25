"""Tests for the ``--debug-e2e`` CLI branch on ``python -m alphamind.scheduler``.

Story ALP-501 (04) layers a new ``run --debug-e2e`` path onto the existing
``_run_once`` shape. The argparse surface gains a flag that is mutually
exclusive with ``--mode live`` and requires ``--once <run_type>``; the
dispatch path constructs a ``DebugE2ESettings`` bundle, wipes + seeds the
synthetic portfolio against the debug DB, records a process-lifetime row,
and calls ``run_invocation`` with ``trigger_source="debug_e2e_cli"``.

The tests exercise the parsing layer directly via ``_parse_args`` and the
execution shape via the ``_run_debug_e2e`` coroutine — same testing seam
the ``--once`` branch uses.
"""

from __future__ import annotations

import contextlib
from typing import Any, cast

import pytest

from alphamind.config.models.run_types import RunType
from alphamind.scheduler.__main__ import _parse_args

# ---------------------------------------------------------------------------
# Argparse surface (acceptance criteria 1-3)
# ---------------------------------------------------------------------------


class TestParseArgsDebugE2E:
    """Validate the new ``--debug-e2e`` flag + its companion validations."""

    def test_debug_e2e_with_once_and_reason_accepted(self) -> None:
        """Happy path: ``--debug-e2e --once <run_type> --reason <text>`` parses cleanly."""
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )
        assert args.debug_e2e is True
        assert args.once == "market_hours_rolling"
        assert args.reason == "test"

    def test_debug_e2e_with_mode_live_raises(self) -> None:
        """``--debug-e2e --mode live`` is mutually exclusive at argparse level."""
        with pytest.raises(SystemExit):
            _parse_args(
                [
                    "run",
                    "--debug-e2e",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                    "--mode",
                    "live",
                ]
            )

    def test_debug_e2e_without_once_raises(self) -> None:
        """``--debug-e2e`` without ``--once`` raises an argparse error."""
        with pytest.raises(SystemExit):
            _parse_args(["run", "--debug-e2e", "--reason", "test"])

    def test_debug_e2e_with_once_but_no_reason_raises(self) -> None:
        """``--debug-e2e --once ...`` still needs ``--reason`` (inherited from ``--once``)."""
        with pytest.raises(SystemExit):
            _parse_args(["run", "--debug-e2e", "--once", "market_hours_rolling"])

    def test_default_debug_e2e_flag_is_false(self) -> None:
        """Omitting ``--debug-e2e`` leaves ``args.debug_e2e`` at ``False``."""
        args = _parse_args(["run", "--once", "market_hours_rolling", "--reason", "test"])
        assert args.debug_e2e is False

    def test_archive_root_flag_parsed_when_paired_with_debug_e2e(self) -> None:
        """``--archive-root`` parses to a ``Path`` when paired with ``--debug-e2e``."""
        from pathlib import Path

        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--archive-root",
                "/tmp/some-archive",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )
        assert args.archive_root == Path("/tmp/some-archive")

    def test_archive_root_default_is_none(self) -> None:
        """Omitting ``--archive-root`` leaves ``args.archive_root`` at ``None``."""
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )
        assert args.archive_root is None

    def test_archive_root_without_debug_e2e_raises(self) -> None:
        """``--archive-root`` without ``--debug-e2e`` errors out at argparse level.

        Production daemons must use the hardcoded ``~/AlphaMind/archive``
        default — the override is debug-e2e-only.
        """
        with pytest.raises(SystemExit):
            _parse_args(
                [
                    "run",
                    "--archive-root",
                    "/tmp/some-archive",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                ]
            )

    def test_fresh_start_flag_parsed_with_debug_e2e(self) -> None:
        """``--fresh-start`` alongside ``--debug-e2e`` parses to ``True`` (ALP-618)."""
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--fresh-start",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )
        assert args.fresh_start is True

    def test_fresh_start_default_is_false(self) -> None:
        """Omitting ``--fresh-start`` leaves ``args.fresh_start`` at ``False``."""
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )
        assert args.fresh_start is False

    def test_fresh_start_without_debug_e2e_requires_once(self) -> None:
        """``--fresh-start`` outside ``--debug-e2e`` triggers the prod
        cold-start bootstrap (ALP-620) — a one-shot operation that
        requires ``--once``. Daemon-mode ``--fresh-start`` is rejected so
        the bootstrap does not silently re-run on every daemon tick.
        """
        with pytest.raises(SystemExit):
            _parse_args(["run", "--fresh-start", "--mode", "paper"])

    def test_fresh_start_without_debug_e2e_with_once_is_allowed(self) -> None:
        """``--fresh-start --once <rt> --reason <text>`` parses cleanly as
        the production cold-start bootstrap (ALP-620)."""
        args = _parse_args(
            [
                "run",
                "--fresh-start",
                "--once",
                "pre_open",
                "--reason",
                "first-run bootstrap",
            ]
        )
        assert args.fresh_start is True
        assert args.debug_e2e is False
        assert args.once == "pre_open"


# ---------------------------------------------------------------------------
# Execution shape — _run_debug_e2e (acceptance criterion 4)
# ---------------------------------------------------------------------------


def _make_fake_venue_config() -> Any:
    """Construct a minimal :class:`VenueConfig` for the CLI tests."""
    from alphamind.config.models.venue import (
        Alpaca,
        AlpacaCredentials,
        SessionHours,
        SessionWindow,
        VenueConfig,
    )

    return VenueConfig(
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


def _build_stub_engine_pair_factory() -> Any:
    """Return a stub ``engine_pair_context`` whose ``async_engine.url.database``
    exposes a deterministic ``-debug-e2e.db`` path so the seeder safety guard
    can be exercised without a real SQLAlchemy engine.
    """
    from alphamind.persistence.session import EnginePair

    class _FakeURL:
        database = "/tmp/whatever-debug-e2e.db"

    class _FakeAsyncEngine:
        url = _FakeURL()

    @contextlib.asynccontextmanager
    async def _async_session_factory_cm() -> Any:
        class _S: ...

        yield _S()

    def _async_session_factory() -> Any:
        return _async_session_factory_cm()

    @contextlib.asynccontextmanager
    async def _stub_engine_pair(path: str | None = None) -> Any:
        yield EnginePair(
            async_engine=cast(Any, _FakeAsyncEngine()),
            async_session_factory=cast(Any, _async_session_factory),
            sync_engine=cast(Any, object()),
            sync_session_factory=cast(Any, object()),
        )

    return _stub_engine_pair


def _make_summary_for_stub() -> Any:
    """Build a canonical :class:`InvocationSummary` for the dispatch stub."""
    from alphamind.execution.write_paths.phase1 import Phase1Summary
    from alphamind.scheduler.orchestrator import InvocationSummary

    return InvocationSummary(
        invocation_id="inv-debug-e2e-1",
        trigger_type="manual",
        trigger_source="debug_e2e_cli",
        firing_run_type=RunType.market_hours_rolling,
        phase1_summary=Phase1Summary(
            fills_processed=0,
            fills_quarantined=0,
            ca_activities_processed=0,
            reconciliation_alerts=0,
        ),
        commands_submitted=0,
        commands_rejected=0,
        staleness_flag=False,
        duration_seconds=0.0,
    )


def _patch_debug_e2e_heavy_setup(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the heavy IO points the debug-e2e dispatch path touches.

    Returns a ``captured`` dict the tests assert against — the call
    sequence (``wipe_and_seed`` before ``record_process_lifetime``, the
    ``run_invocation`` kwargs) is captured here so each AC reads off the
    same recording.
    """
    from alphamind.scheduler import __main__ as module
    from alphamind.scheduler.debug_e2e import seed as seed_module
    from alphamind.scheduler.debug_e2e import settings as settings_module
    from alphamind.scheduler.debug_e2e.broker import (
        LogOnlyAccountStateQueries,
        LogOnlyCorporateActionsQueries,
    )
    from tests.scheduler.test_progress import RecordingProgressEmitter

    shared_recorder = RecordingProgressEmitter()
    captured: dict[str, Any] = {
        "events": [],
        "wipe_seed_calls": [],
        "run_invocation_kwargs": None,
        "recorder": shared_recorder,
        "configure_debug_e2e_archive_root": None,
    }

    monkeypatch.setattr(module, "_load_venue_config", lambda _config_dir: _make_fake_venue_config())
    monkeypatch.setattr(module, "configure_pipeline_logging", lambda: None)

    async def _stub_process_lifetime(**_kwargs: Any) -> str:
        captured["events"].append("record_process_lifetime")
        return "proc-debug-e2e-1"

    monkeypatch.setattr(module, "record_process_lifetime", _stub_process_lifetime)
    monkeypatch.setattr(module, "engine_pair_context", _build_stub_engine_pair_factory())

    async def _stub_wipe_and_seed(**kwargs: Any) -> None:
        captured["wipe_seed_calls"].append(kwargs)
        captured["events"].append("wipe_and_seed")

    monkeypatch.setattr(seed_module, "wipe_and_seed", _stub_wipe_and_seed)

    def _stub_configure_debug_e2e(*, archive_root: Any, portfolio: Any) -> Any:
        captured["configure_debug_e2e_archive_root"] = archive_root
        captured["configure_debug_e2e_portfolio"] = portfolio
        return settings_module.DebugE2ESettings(
            account_queries=LogOnlyAccountStateQueries(portfolio),
            ca_queries=LogOnlyCorporateActionsQueries(),
            emitter_factory=lambda _invocation_id: shared_recorder,
        )

    monkeypatch.setattr(settings_module, "configure_debug_e2e", _stub_configure_debug_e2e)

    async def _stub_run_invocation(**kwargs: Any) -> Any:
        captured["run_invocation_kwargs"] = kwargs
        captured["events"].append("run_invocation")
        return _make_summary_for_stub()

    monkeypatch.setattr(module, "run_invocation", _stub_run_invocation)

    return captured


class TestRunDebugE2E:
    """Verify the ``_run_debug_e2e`` dispatch shape end-to-end."""

    async def test_seed_phase_event_emitted_before_run_invocation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``phase_start("seed")``/``phase_done("seed")`` wrap ``wipe_and_seed``."""
        from alphamind.scheduler.__main__ import _run_debug_e2e

        captured = _patch_debug_e2e_heavy_setup(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )

        await _run_debug_e2e(args)

        recorder = captured["recorder"]
        events = [kind for kind, _fields in recorder.events]
        # The seed phase must wrap wipe_and_seed.
        assert ("phase_start", {"phase": "seed"}) in recorder.events
        seed_done_indices = [
            i
            for i, evt in enumerate(recorder.events)
            if evt[0] == "phase_done" and evt[1].get("phase") == "seed"
        ]
        seed_start_indices = [
            i
            for i, evt in enumerate(recorder.events)
            if evt[0] == "phase_start" and evt[1].get("phase") == "seed"
        ]
        assert seed_start_indices, f"phase_start('seed') missing from {events}"
        assert seed_done_indices, f"phase_done('seed') missing from {events}"
        assert seed_start_indices[0] < seed_done_indices[0], (
            "phase_done('seed') fired before phase_start('seed')"
        )

    async def test_wipe_and_seed_runs_before_record_process_lifetime(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``wipe_and_seed`` must execute before ``record_process_lifetime``.

        The seeder wipes the ``process_lifetimes`` table; if it ran *after*
        the row insert, the row would disappear and Phase 1's FK to
        ``process_lifetime_id`` would fail.
        """
        from alphamind.scheduler.__main__ import _run_debug_e2e

        captured = _patch_debug_e2e_heavy_setup(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )
        await _run_debug_e2e(args)

        events = captured["events"]
        seed_idx = events.index("wipe_and_seed")
        proc_idx = events.index("record_process_lifetime")
        assert seed_idx < proc_idx, (
            f"wipe_and_seed must precede record_process_lifetime; got {events}"
        )

    async def test_run_invocation_invoked_with_debug_e2e_trigger_source(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run_invocation`` is called once with the canonical trigger_source + debug_e2e set."""
        from alphamind.scheduler.__main__ import _run_debug_e2e

        captured = _patch_debug_e2e_heavy_setup(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )
        await _run_debug_e2e(args)

        kwargs = captured["run_invocation_kwargs"]
        assert kwargs is not None
        assert kwargs["trigger_source"] == "debug_e2e_cli"
        assert kwargs["trigger_type"] == "manual"
        assert kwargs["trigger_reason"] == "smoke"
        assert kwargs["firing_run_type"] is RunType.market_hours_rolling
        # The context must carry the debug-e2e settings bundle.
        assert kwargs["context"].debug_e2e is not None

    async def test_wipe_and_seed_receives_db_path_from_engine_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``db_path`` passed to ``wipe_and_seed`` is derived from the engine URL.

        Story body § Surfacing conditions: when ``engines.db_path`` is
        absent (the current ``EnginePair`` shape), the helper derives the
        path from ``engines.async_engine.url.database`` to preserve the
        seeder's safety guard.
        """
        from alphamind.scheduler.__main__ import _run_debug_e2e

        captured = _patch_debug_e2e_heavy_setup(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )
        await _run_debug_e2e(args)

        call = captured["wipe_seed_calls"][0]
        assert call["db_path"] == "/tmp/whatever-debug-e2e.db"

    async def test_wipe_and_seed_runtime_error_logged_with_db_path_and_reraised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing safety-guard ``RuntimeError`` surfaces the refusing path.

        The seeder's ``-debug-e2e.db`` suffix guard raises
        :class:`RuntimeError` on a mis-set ``DATABASE_PATH``. Without
        explicit handling the error rolls up into the generic
        "pipeline scheduler exited with error" frame, hiding the
        actionable path. The dispatch must log the path before
        re-raising.
        """
        import logging

        from alphamind.scheduler import __main__ as module
        from alphamind.scheduler.__main__ import _run_debug_e2e
        from alphamind.scheduler.debug_e2e import seed as seed_module

        _patch_debug_e2e_heavy_setup(monkeypatch)

        async def _raising_wipe(**_kwargs: Any) -> None:
            msg = "refusing to wipe non-debug DB path '/tmp/whatever-debug-e2e.db'"
            raise RuntimeError(msg)

        monkeypatch.setattr(seed_module, "wipe_and_seed", _raising_wipe)

        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )

        with (
            caplog.at_level(logging.ERROR, logger=module.log.name),
            pytest.raises(RuntimeError, match="refusing to wipe non-debug DB path"),
        ):
            await _run_debug_e2e(args)

        # The error log names db_path so operators see the refusing path
        # before the outermost ``BaseException`` frame swallows it.
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("db_path" in r.getMessage() for r in error_records)
        assert any("-debug-e2e.db" in r.getMessage() for r in error_records)

    async def test_archive_root_override_threads_to_configure_and_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """``--archive-root`` propagates to ``configure_debug_e2e`` + the context.

        The verify-script's --archive-root must reach
        :func:`configure_debug_e2e` (so the JSONL emitter writes under
        the operator-chosen verification archive) and the
        :class:`RunInvocationContext` (so the orchestrator writes
        ``resolved_config.json`` under the same root).
        """
        from alphamind.scheduler.__main__ import _run_debug_e2e

        override = tmp_path / "verify-archive"
        captured = _patch_debug_e2e_heavy_setup(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--archive-root",
                str(override),
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )
        await _run_debug_e2e(args)

        assert captured["configure_debug_e2e_archive_root"] == override
        assert captured["run_invocation_kwargs"]["context"].archive_root == override
        assert override.is_dir()

    async def test_default_portfolio_is_synthetic(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without ``--fresh-start``, the managed ``SYNTHETIC_PORTFOLIO`` is used (ALP-618)."""
        from alphamind.scheduler.__main__ import _run_debug_e2e
        from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO

        captured = _patch_debug_e2e_heavy_setup(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )
        await _run_debug_e2e(args)

        # Both seams (configure_debug_e2e + wipe_and_seed) must see the same
        # portfolio so the broker stand-in projects what the seeder wrote.
        assert captured["configure_debug_e2e_portfolio"] is SYNTHETIC_PORTFOLIO
        assert captured["wipe_seed_calls"][0]["portfolio"] is SYNTHETIC_PORTFOLIO

    async def test_fresh_start_threads_fresh_start_portfolio(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--fresh-start`` swaps in ``FRESH_START_PORTFOLIO`` at both seams (ALP-618)."""
        from alphamind.scheduler.__main__ import _run_debug_e2e
        from alphamind.scheduler.debug_e2e.portfolio import FRESH_START_PORTFOLIO

        captured = _patch_debug_e2e_heavy_setup(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--fresh-start",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )
        await _run_debug_e2e(args)

        assert captured["configure_debug_e2e_portfolio"] is FRESH_START_PORTFOLIO
        assert captured["wipe_seed_calls"][0]["portfolio"] is FRESH_START_PORTFOLIO
