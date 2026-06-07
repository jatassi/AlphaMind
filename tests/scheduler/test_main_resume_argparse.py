"""Tests for the ``--resume-from`` argparse surface on ``scheduler run`` (ALP-693).

The argparse layer is the first line of defense — every rejection arm
listed in the design doc § 6 (failure modes table) must surface here as
exit code 2 with a named-cause stderr message, before any DB write or
``wipe_and_seed`` call.

Tests exercise ``_parse_args`` directly for the synchronous argparse
rejections, and the dispatch entry point ``_run_debug_e2e`` (with heavy
IO stubbed) for the load-time validation arms that fire after argparse
parses but before ``wipe_and_seed`` runs.

The happy path (``--resume-from`` accepted alongside ``--debug-e2e``
plus a complete source archive) is exercised by:
- :func:`test_resume_from_valid_argparse_accepted` — the parse layer
  accepts the flag and records the colon-separated parts.
- :func:`test_resume_from_threads_resume_context_into_configure_debug_e2e`
  — the dispatch path validates the source archive and passes the
  resulting :class:`ResumeContext` to ``configure_debug_e2e``, which
  populates :attr:`DebugE2ESettings.resume_context`.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from alphamind._kernel.archive_layout import invocation_archive_dir
from alphamind.config.models.run_types import RunType
from alphamind.scheduler.__main__ import _parse_args

_INVOCATION_AS_OF = datetime(2026, 5, 26, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Synchronous argparse-layer rejections
# ---------------------------------------------------------------------------


class TestParseArgsResumeFrom:
    """Argparse-layer rejection of malformed ``--resume-from`` invocations."""

    def test_resume_from_valid_argparse_accepted(self) -> None:
        """Happy path: ``--debug-e2e --resume-from <id>:<phase>`` parses cleanly."""
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--resume-from",
                "inv-20260526T000000Z-abc:analyst",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )
        assert args.resume_from == "inv-20260526T000000Z-abc:analyst"

    def test_resume_from_default_is_none(self) -> None:
        """Omitting ``--resume-from`` leaves ``args.resume_from`` at ``None``."""
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
        assert args.resume_from is None

    def test_resume_from_without_debug_e2e_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--resume-from`` without ``--debug-e2e`` exits 2 with a named cause."""
        with pytest.raises(SystemExit) as excinfo:
            _parse_args(
                [
                    "run",
                    "--resume-from",
                    "inv-x:analyst",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                ]
            )
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "--resume-from" in captured.err
        assert "--debug-e2e" in captured.err

    def test_resume_from_with_fresh_start_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--resume-from`` is mutually exclusive with ``--fresh-start``.

        Resume against a different portfolio fixture is a category error
        (parent decision (F)) — the prior archive's outputs were
        produced against the fixture the original run used.
        """
        with pytest.raises(SystemExit) as excinfo:
            _parse_args(
                [
                    "run",
                    "--debug-e2e",
                    "--fresh-start",
                    "--resume-from",
                    "inv-x:analyst",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                ]
            )
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "--resume-from" in captured.err
        assert "--fresh-start" in captured.err

    def test_resume_from_missing_colon_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--resume-from foo`` (no colon) exits 2 with a named cause."""
        with pytest.raises(SystemExit) as excinfo:
            _parse_args(
                [
                    "run",
                    "--debug-e2e",
                    "--resume-from",
                    "no-colon-here",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                ]
            )
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "--resume-from" in captured.err

    def test_resume_from_empty_invocation_id_raises(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--resume-from :phase`` (empty invocation_id) exits 2."""
        with pytest.raises(SystemExit) as excinfo:
            _parse_args(
                [
                    "run",
                    "--debug-e2e",
                    "--resume-from",
                    ":analyst",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                ]
            )
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "--resume-from" in captured.err

    def test_resume_from_empty_phase_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--resume-from inv:`` (empty phase) exits 2."""
        with pytest.raises(SystemExit) as excinfo:
            _parse_args(
                [
                    "run",
                    "--debug-e2e",
                    "--resume-from",
                    "inv:",
                    "--once",
                    "market_hours_rolling",
                    "--reason",
                    "test",
                ]
            )
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "--resume-from" in captured.err


# ---------------------------------------------------------------------------
# Dispatch-layer rejections (after argparse parses, before wipe_and_seed)
#
# Argparse only sees the flag's string value — it can't probe the disk
# without coupling argparse parsing to filesystem state. The unknown-phase
# and missing-source-dir arms therefore fire from inside _run_debug_e2e
# right before configure_debug_e2e is called; the dispatch catches
# ResumeValidationError and exits 2 with the message on stderr.
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
    """Return a stub ``engine_pair_context`` that yields a fake EnginePair.

    Used only by the happy-path test that drives ``_run_debug_e2e`` to
    completion; the rejection-arm tests fail before any engine is
    constructed so they don't need this seam.
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
    from alphamind.execution.write_paths.fill_collection import FillCollectionSummary
    from alphamind.scheduler.orchestrator import InvocationSummary

    return InvocationSummary(
        invocation_id="inv-debug-e2e-resume",
        trigger_type="manual",
        trigger_source="debug_e2e_cli",
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
        duration_seconds=0.0,
    )


def _patch_debug_e2e_heavy_setup_for_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Stub the heavy IO points the debug-e2e dispatch path touches.

    Mirrors the shape of ``_patch_debug_e2e_heavy_setup`` in
    ``test_main_debug_e2e.py`` but captures the ``resume_context``
    kwarg flowing through ``configure_debug_e2e``.
    """
    from alphamind.scheduler import __main__ as module
    from alphamind.scheduler.debug_e2e import seed as seed_module
    from alphamind.scheduler.debug_e2e import settings as settings_module
    from alphamind.scheduler.debug_e2e.broker import (
        LogOnlyAccountStateQueries,
        LogOnlyBatchQuoteSource,
        LogOnlyCorporateActionsQueries,
    )
    from tests.scheduler.test_progress import RecordingProgressEmitter

    shared_recorder = RecordingProgressEmitter()
    captured: dict[str, Any] = {
        "events": [],
        "run_invocation_kwargs": None,
        "configure_debug_e2e_kwargs": None,
    }

    monkeypatch.setattr(module, "_load_venue_config", lambda _config_dir: _make_fake_venue_config())
    monkeypatch.setattr(module, "configure_pipeline_logging", lambda: None)

    async def _stub_process_lifetime(**_kwargs: Any) -> str:
        captured["events"].append("record_process_lifetime")
        return "proc-resume-1"

    monkeypatch.setattr(module, "record_process_lifetime", _stub_process_lifetime)
    monkeypatch.setattr(module, "engine_pair_context", _build_stub_engine_pair_factory())

    async def _stub_wipe_and_seed(**_kwargs: Any) -> None:
        captured["events"].append("wipe_and_seed")

    monkeypatch.setattr(seed_module, "wipe_and_seed", _stub_wipe_and_seed)

    def _stub_configure_debug_e2e(
        *, archive_root: Any, portfolio: Any, resume_context: Any = None
    ) -> Any:
        captured["configure_debug_e2e_kwargs"] = {
            "archive_root": archive_root,
            "portfolio": portfolio,
            "resume_context": resume_context,
        }
        return settings_module.DebugE2ESettings(
            account_queries=LogOnlyAccountStateQueries(portfolio),
            ca_queries=LogOnlyCorporateActionsQueries(),
            quote_source=LogOnlyBatchQuoteSource(),
            emitter_factory=lambda _invocation_id, _as_of: shared_recorder,
            resume_context=resume_context,
        )

    monkeypatch.setattr(settings_module, "configure_debug_e2e", _stub_configure_debug_e2e)

    async def _stub_run_invocation(**kwargs: Any) -> Any:
        captured["run_invocation_kwargs"] = kwargs
        captured["events"].append("run_invocation")
        return _make_summary_for_stub()

    monkeypatch.setattr(module, "run_invocation", _stub_run_invocation)

    return captured


def _seed_source_archive(
    *,
    archive_root: Path,
    invocation_id: str,
    phase_files: list[str],
) -> Path:
    """Materialize an archive directory with the named phase_outputs files."""
    invocation_dir = invocation_archive_dir(
        archive_root=archive_root,
        as_of=_INVOCATION_AS_OF,
        invocation_id=invocation_id,
    )
    phase_outputs_dir = invocation_dir / "phase_outputs"
    phase_outputs_dir.mkdir(parents=True)
    for phase in phase_files:
        (phase_outputs_dir / f"{phase}.json").write_text("{}", encoding="utf-8")
    return invocation_dir


class TestResumeFromDispatchLayerRejections:
    """Dispatch-layer rejection arms: argparse passes, then load_resume_context fails.

    The dispatch path calls ``load_resume_context`` after argparse parses
    the colon-separated value and before ``configure_debug_e2e`` is
    called. A ``ResumeValidationError`` from the loader becomes
    ``SystemExit(2)`` with the message on stderr — the seeder and engine
    pair are never opened.
    """

    async def test_unknown_phase_raises_exit_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--resume-from inv:bogus`` exits 2 with stderr naming the bad phase."""
        from alphamind.scheduler.__main__ import _run_debug_e2e

        # Seed a valid source-dir so the only failing check is unknown phase.
        invocation_id = "inv-unknown-phase"
        _seed_source_archive(
            archive_root=tmp_path,
            invocation_id=invocation_id,
            phase_files=[],
        )

        captured_events = _patch_debug_e2e_heavy_setup_for_resume(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--archive-root",
                str(tmp_path),
                "--resume-from",
                f"{invocation_id}:not_a_phase",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )

        with pytest.raises(SystemExit) as excinfo:
            await _run_debug_e2e(args)
        assert excinfo.value.code == 2

        captured = capsys.readouterr()
        assert "not_a_phase" in captured.err
        # Argparse-time rejection: configure_debug_e2e never called,
        # wipe_and_seed never ran, no DB writes.
        assert captured_events["configure_debug_e2e_kwargs"] is None
        assert "wipe_and_seed" not in captured_events["events"]

    async def test_missing_source_directory_raises_exit_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--resume-from missing-id:analyst`` exits 2 with stderr naming the dir."""
        from alphamind.scheduler.__main__ import _run_debug_e2e

        captured_events = _patch_debug_e2e_heavy_setup_for_resume(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--archive-root",
                str(tmp_path),
                "--resume-from",
                "missing-source-id:analyst",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )

        with pytest.raises(SystemExit) as excinfo:
            await _run_debug_e2e(args)
        assert excinfo.value.code == 2

        captured = capsys.readouterr()
        assert "missing-source-id" in captured.err
        # No DB writes happened.
        assert captured_events["configure_debug_e2e_kwargs"] is None
        assert "wipe_and_seed" not in captured_events["events"]

    async def test_missing_upstream_phase_output_raises_exit_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing upstream phase output exits 2; stderr names the earliest valid target."""
        from alphamind.scheduler.__main__ import _run_debug_e2e

        # Seed only the 4 leaf-SDK phases — synthesizer is missing, so
        # analyst's upstream is incomplete.  The earliest valid target
        # the archive does cover is ``adaptive``.
        invocation_id = "inv-only-leaves"
        _seed_source_archive(
            archive_root=tmp_path,
            invocation_id=invocation_id,
            phase_files=["tech_semis", "financials", "energy", "qualitative"],
        )

        _patch_debug_e2e_heavy_setup_for_resume(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--archive-root",
                str(tmp_path),
                "--resume-from",
                f"{invocation_id}:analyst",
                "--once",
                "market_hours_rolling",
                "--reason",
                "test",
            ]
        )

        with pytest.raises(SystemExit) as excinfo:
            await _run_debug_e2e(args)
        assert excinfo.value.code == 2

        captured = capsys.readouterr()
        # The named-cause message must include both the missing phase
        # (synthesizer) and the earliest valid resume target (adaptive).
        assert "synthesizer" in captured.err
        assert "adaptive" in captured.err


# ---------------------------------------------------------------------------
# Happy path: valid --resume-from threads ResumeContext through to settings
# ---------------------------------------------------------------------------


class TestResumeFromValid:
    """The happy path: argparse parses + load succeeds + ResumeContext threaded."""

    async def test_resume_from_threads_resume_context_into_configure_debug_e2e(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A valid ``--resume-from`` propagates a ResumeContext to ``configure_debug_e2e``."""
        from alphamind.scheduler.__main__ import _run_debug_e2e
        from alphamind.scheduler.debug_e2e.resume import ResumeContext, phases_to_replay

        invocation_id = "inv-happy"
        upstream = sorted(phases_to_replay("analyst"))
        _seed_source_archive(
            archive_root=tmp_path,
            invocation_id=invocation_id,
            phase_files=upstream,
        )

        captured_events = _patch_debug_e2e_heavy_setup_for_resume(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--archive-root",
                str(tmp_path),
                "--resume-from",
                f"{invocation_id}:analyst",
                "--once",
                "market_hours_rolling",
                "--reason",
                "smoke",
            ]
        )

        await _run_debug_e2e(args)

        # configure_debug_e2e must have been called with a populated
        # ResumeContext whose source_archive_dir + resume_phase mirror
        # the CLI input.
        kwargs = captured_events["configure_debug_e2e_kwargs"]
        assert kwargs is not None
        ctx = kwargs["resume_context"]
        assert isinstance(ctx, ResumeContext)
        assert ctx.resume_phase == "analyst"
        assert ctx.source_archive_dir == invocation_archive_dir(
            archive_root=tmp_path,
            as_of=_INVOCATION_AS_OF,
            invocation_id=invocation_id,
        )
        assert ctx.phases_to_replay == phases_to_replay("analyst")

        # And the orchestrator context carries the populated settings.
        run_kwargs = captured_events["run_invocation_kwargs"]
        assert run_kwargs is not None
        assert run_kwargs["context"].debug_e2e is not None
        assert run_kwargs["context"].debug_e2e.resume_context is ctx

    async def test_no_resume_from_keeps_resume_context_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Omitting ``--resume-from`` leaves ``DebugE2ESettings.resume_context = None``."""
        from alphamind.scheduler.__main__ import _run_debug_e2e

        captured_events = _patch_debug_e2e_heavy_setup_for_resume(monkeypatch)
        args = _parse_args(
            [
                "run",
                "--debug-e2e",
                "--archive-root",
                str(tmp_path),
                "--once",
                "market_hours_rolling",
                "--reason",
                "fresh-run",
            ]
        )

        await _run_debug_e2e(args)

        kwargs = captured_events["configure_debug_e2e_kwargs"]
        assert kwargs is not None
        assert kwargs["resume_context"] is None
        run_kwargs = captured_events["run_invocation_kwargs"]
        assert run_kwargs["context"].debug_e2e.resume_context is None
