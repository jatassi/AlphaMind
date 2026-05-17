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
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from tests.scheduler.test_progress import RecordingProgressEmitter

    shared_recorder = RecordingProgressEmitter()
    captured: dict[str, Any] = {
        "events": [],
        "wipe_seed_calls": [],
        "run_invocation_kwargs": None,
        "recorder": shared_recorder,
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

    def _stub_configure_debug_e2e(*, archive_root: Any) -> Any:
        return settings_module.DebugE2ESettings(
            account_queries=LogOnlyAccountStateQueries(SYNTHETIC_PORTFOLIO),
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
