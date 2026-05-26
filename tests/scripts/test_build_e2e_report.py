"""Tests for ``scripts/build_e2e_report.py`` (story 05 / ALP-502).

The HTML report builder was rewritten in ALP-502 to consume the
debug-e2e single-invocation archive (``progress.jsonl`` +
``resolved_config.json`` + ``pipeline.log``) instead of the per-phase
verify archives the legacy version stitched together. The module-level
render helpers each take small typed inputs so this suite can exercise
the rendered HTML without driving a real ``--debug-e2e`` subprocess.

The script lives under ``scripts/`` and is imported via
``importlib.util.spec_from_file_location`` for the same reason
``verify_debug_e2e.py`` is.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from alphamind._kernel.archive_layout import invocation_archive_dir

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_e2e_report.py"


@pytest.fixture(scope="module")
def report_module() -> ModuleType:
    """Load ``scripts/build_e2e_report.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("build_e2e_report", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_e2e_report"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers for assembling synthetic fixture archives
# ---------------------------------------------------------------------------


_T0 = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
_INVOCATION_AS_OF = datetime(2026, 5, 26, tzinfo=UTC)


def _ts(offset_s: float) -> str:
    return (_T0 + timedelta(seconds=offset_s)).isoformat()


def _full_progress_stream() -> list[dict[str, Any]]:
    """12 in-invocation phase pairs + 9 SDK call pairs in dependency-respecting order.

    The pre-invocation ``seed`` event is intentionally absent — it lives
    in the sibling ``_pre_invocation`` archive directory per parent
    issue ALP-493 § D. Distillation is the deterministic numerical
    orchestrator and emits no SDK call (parent issue § E).
    """
    s: list[dict[str, Any]] = []

    def phase(name: str, start: float, done: float) -> None:
        s.append({"event": "phase_start", "phase": name, "timestamp": _ts(start)})
        s.append({"event": "phase_done", "phase": name, "timestamp": _ts(done)})

    def sdk_pair(
        *,
        phase_name: str,
        agent: str,
        model: str,
        req: float,
        resp: float,
        input_tokens: int,
        output_tokens: int,
        tool_calls: int,
        duration_s: float | None = None,
        stop_reason: str = "end_turn",
    ) -> None:
        s.append(
            {
                "event": "agent_request",
                "phase": phase_name,
                "agent": agent,
                "model": model,
                "timestamp": _ts(req),
            }
        )
        s.append(
            {
                "event": "agent_response",
                "phase": phase_name,
                "agent": agent,
                "model": model,
                "duration_s": duration_s if duration_s is not None else (resp - req),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tool_calls": tool_calls,
                "stop_reason": stop_reason,
                "timestamp": _ts(resp),
            }
        )

    phase("phase1", 2, 3)
    phase("snapshot_assembly", 4, 5)

    s.append({"event": "phase_start", "phase": "distillation", "timestamp": _ts(6)})
    s.append({"event": "phase_done", "phase": "distillation", "timestamp": _ts(13)})

    s.append({"event": "phase_start", "phase": "domain_researchers", "timestamp": _ts(14)})
    s.append({"event": "phase_start", "phase": "qualitative", "timestamp": _ts(14)})
    sdk_pair(
        phase_name="domain_researchers",
        agent="tech_semis_researcher",
        model="claude-sonnet-4-5",
        req=15,
        resp=30,
        input_tokens=8000,
        output_tokens=1500,
        tool_calls=0,
    )
    sdk_pair(
        phase_name="domain_researchers",
        agent="financials_researcher",
        model="claude-sonnet-4-5",
        req=16,
        resp=32,
        input_tokens=8500,
        output_tokens=1700,
        tool_calls=0,
    )
    sdk_pair(
        phase_name="domain_researchers",
        agent="energy_researcher",
        model="claude-sonnet-4-5",
        req=17,
        resp=33,
        input_tokens=7500,
        output_tokens=1400,
        tool_calls=0,
    )
    sdk_pair(
        phase_name="qualitative",
        agent="qualitative_researcher",
        model="claude-sonnet-4-5",
        req=18,
        resp=35,
        input_tokens=7000,
        output_tokens=800,
        tool_calls=5,
    )
    s.append({"event": "phase_done", "phase": "qualitative", "timestamp": _ts(36)})
    s.append({"event": "phase_done", "phase": "domain_researchers", "timestamp": _ts(37)})

    s.append({"event": "phase_start", "phase": "adaptive", "timestamp": _ts(38)})
    sdk_pair(
        phase_name="adaptive",
        agent="adaptive_researcher",
        model="claude-sonnet-4-5",
        req=39,
        resp=70,
        input_tokens=9500,
        output_tokens=1200,
        tool_calls=10,
    )
    s.append({"event": "phase_done", "phase": "adaptive", "timestamp": _ts(71)})

    s.append({"event": "phase_start", "phase": "synthesizer", "timestamp": _ts(72)})
    sdk_pair(
        phase_name="synthesizer",
        agent="synthesizer",
        model="claude-sonnet-4-5",
        req=73,
        resp=90,
        input_tokens=11000,
        output_tokens=1800,
        tool_calls=0,
    )
    s.append({"event": "phase_done", "phase": "synthesizer", "timestamp": _ts(91)})

    s.append({"event": "phase_start", "phase": "analyst", "timestamp": _ts(92)})
    s.append({"event": "phase_start", "phase": "strategist", "timestamp": _ts(92)})
    sdk_pair(
        phase_name="analyst",
        agent="analyst",
        model="claude-opus-4-1",
        req=93,
        resp=153,
        input_tokens=15000,
        output_tokens=4000,
        tool_calls=2,
    )
    s.append({"event": "phase_done", "phase": "analyst", "timestamp": _ts(154)})
    sdk_pair(
        phase_name="strategist",
        agent="strategist",
        model="claude-opus-4-1",
        req=94,
        resp=694,
        input_tokens=30000,
        output_tokens=40000,
        tool_calls=0,
    )
    s.append({"event": "phase_done", "phase": "strategist", "timestamp": _ts(695)})

    phase("pre_processor", 696, 697)

    s.append({"event": "phase_start", "phase": "pm", "timestamp": _ts(698)})
    sdk_pair(
        phase_name="pm",
        agent="portfolio_manager",
        model="claude-opus-4-1",
        req=699,
        resp=1059,
        input_tokens=35000,
        output_tokens=30000,
        tool_calls=8,
    )
    s.append({"event": "phase_done", "phase": "pm", "timestamp": _ts(1060)})

    phase("phase2", 1061, 1062)
    return s


def _truncated_progress_stream() -> list[dict[str, Any]]:
    """Stop after `pm` phase_start emits but before its phase_done — simulates a hang."""
    s = _full_progress_stream()
    pm_done_idx = next(
        i for i, e in enumerate(s) if e.get("event") == "phase_done" and e.get("phase") == "pm"
    )
    # Drop pm phase_done + phase2 events.
    return s[:pm_done_idx]


def _make_archive(
    tmp_path: Path,
    *,
    invocation_id: str = "20260516T120000Z-debug-e2e",
    progress_events: list[dict[str, Any]] | None = None,
    resolved_config: dict[str, Any] | None = None,
    pipeline_log: str | None = None,
) -> tuple[Path, str]:
    """Build a synthetic archive layout under ``tmp_path``."""
    inv_dir = invocation_archive_dir(
        archive_root=tmp_path,
        as_of=_INVOCATION_AS_OF,
        invocation_id=invocation_id,
    )
    inv_dir.mkdir(parents=True)

    progress = inv_dir / "progress.jsonl"
    events = progress_events if progress_events is not None else _full_progress_stream()
    with progress.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, sort_keys=True) + "\n")

    config_payload = resolved_config or {
        "active_profile": "balanced",
        "active_regime": "neutral",
        "active_mode": "paper",
        "trigger_source": "debug_e2e_cli",
        "firing_run_type": "market_hours_rolling",
    }
    (inv_dir / "resolved_config.json").write_text(
        json.dumps(config_payload, indent=2), encoding="utf-8"
    )

    if pipeline_log is not None:
        (inv_dir / "pipeline.log").write_text(pipeline_log, encoding="utf-8")

    return tmp_path, invocation_id


# ---------------------------------------------------------------------------
# load_invocation_archive
# ---------------------------------------------------------------------------


def test_load_invocation_archive_returns_typed_view(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """The loader collects progress events + resolved config + log text."""
    archive_root, invocation_id = _make_archive(tmp_path, pipeline_log="INFO ok\n")

    archive = report_module.load_invocation_archive(
        archive_root=archive_root, invocation_id=invocation_id
    )
    assert archive.invocation_id == invocation_id
    assert archive.archive_root == archive_root
    # 12 phase_start + 12 phase_done + 9 agent_request + 9 agent_response
    # = 42 events in the canonical fixture above (1 phase_start emitted per
    # in-invocation phase, 1 phase_done per phase, plus 9 SDK
    # request/response pairs).
    assert len(archive.events) > 0
    assert archive.resolved_config["active_profile"] == "balanced"
    assert "INFO ok" in archive.pipeline_log


def test_load_invocation_archive_handles_missing_log(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """A missing ``pipeline.log`` is acceptable — the field is empty."""
    archive_root, invocation_id = _make_archive(tmp_path, pipeline_log=None)

    archive = report_module.load_invocation_archive(
        archive_root=archive_root, invocation_id=invocation_id
    )
    assert archive.pipeline_log == ""


# ---------------------------------------------------------------------------
# discover_invocation_id
# ---------------------------------------------------------------------------


def test_discover_invocation_id_returns_the_sole_invocation_directory(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """Single ``invocations/*`` directory is auto-discovered."""
    archive_root, invocation_id = _make_archive(tmp_path)

    found = report_module.discover_invocation_id(archive_root)
    assert found == invocation_id


def test_discover_invocation_id_raises_on_multiple(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """Multiple invocation directories require explicit ``--invocation-id``."""
    _make_archive(tmp_path, invocation_id="run-a")
    _make_archive(tmp_path, invocation_id="run-b")

    with pytest.raises(ValueError, match="multiple"):
        report_module.discover_invocation_id(tmp_path)


def test_discover_invocation_id_raises_on_zero(report_module: ModuleType, tmp_path: Path) -> None:
    """An empty archive root yields a clear error."""
    (tmp_path / "2026-05-26").mkdir()
    with pytest.raises(ValueError, match="no invocations"):
        report_module.discover_invocation_id(tmp_path)


def test_discover_invocation_id_excludes_leading_underscore_dirs(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """Auto-discovery ignores ``_pre_invocation`` and any leading-underscore directory.

    The debug-e2e CLI writes the pre-invocation ``seed`` event to
    ``<archive>/invocations/_pre_invocation/progress.jsonl`` alongside
    the canonical ``<invocation_id>`` directory. Without the leading-
    underscore exclusion, ``discover_invocation_id`` errors out with
    "multiple invocations" and breaks the auto-discovery contract.
    """
    archive_root, invocation_id = _make_archive(tmp_path)
    date_part = _INVOCATION_AS_OF.strftime("%Y-%m-%d")
    (tmp_path / date_part / "_pre_invocation").mkdir()

    found = report_module.discover_invocation_id(archive_root)
    assert found == invocation_id


# ---------------------------------------------------------------------------
# build_phase_summaries
# ---------------------------------------------------------------------------


def test_build_phase_summaries_marks_all_in_invocation_phases_passing_on_full_stream(
    report_module: ModuleType,
) -> None:
    """Every in-invocation phase carrying both start + done renders as PASS."""
    events = _full_progress_stream()
    summaries = report_module.build_phase_summaries(events)
    assert len(summaries) == 12
    for s in summaries:
        assert s.started is True
        assert s.done is True
        assert s.verdict == "PASS"
        assert s.duration_s is not None and s.duration_s >= 0


def test_build_phase_summaries_marks_unfinished_phase_as_incomplete(
    report_module: ModuleType,
) -> None:
    """A phase with no ``phase_done`` event renders FAIL."""
    events = _truncated_progress_stream()
    summaries = {s.name: s for s in report_module.build_phase_summaries(events)}
    # pm started but never finished.
    assert summaries["pm"].started is True
    assert summaries["pm"].done is False
    assert summaries["pm"].verdict == "FAIL"
    # phase2 never even started.
    assert summaries["phase2"].started is False
    assert summaries["phase2"].verdict == "FAIL"


# ---------------------------------------------------------------------------
# build_sdk_call_rows
# ---------------------------------------------------------------------------


def test_build_sdk_call_rows_yields_one_row_per_agent_request_response(
    report_module: ModuleType,
) -> None:
    """Each ``(phase, agent)`` request/response pair becomes one table row."""
    events = _full_progress_stream()
    rows = report_module.build_sdk_call_rows(events)
    keys = [(r.phase, r.agent) for r in rows]
    assert ("domain_researchers", "tech_semis_researcher") in keys
    assert ("domain_researchers", "financials_researcher") in keys
    assert ("domain_researchers", "energy_researcher") in keys
    assert ("qualitative", "qualitative_researcher") in keys
    assert ("adaptive", "adaptive_researcher") in keys
    assert ("synthesizer", "synthesizer") in keys
    assert ("analyst", "analyst") in keys
    assert ("strategist", "strategist") in keys
    assert ("pm", "portfolio_manager") in keys
    assert len(rows) == 9  # 9 SDK call pairs per parent ALP-493 § E


def test_build_sdk_call_rows_carries_response_metrics(
    report_module: ModuleType,
) -> None:
    """Each row carries `duration_s`, `input_tokens`, `output_tokens`, etc."""
    events = _full_progress_stream()
    rows = report_module.build_sdk_call_rows(events)
    pm_row = next(r for r in rows if r.agent == "portfolio_manager")
    assert pm_row.input_tokens == 35000
    assert pm_row.output_tokens == 30000
    assert pm_row.tool_calls == 8
    assert pm_row.stop_reason == "end_turn"
    assert pm_row.duration_s == pytest.approx(360.0)
    assert pm_row.model == "claude-opus-4-1"


def test_build_sdk_call_rows_marks_orphan_request_as_pending(
    report_module: ModuleType,
) -> None:
    """A request with no matching response renders as ``pending``."""
    events = _full_progress_stream()
    # Drop the pm response and phase_done — leaves a pending request.
    events = [
        e for e in events if not (e.get("event") == "agent_response" and e.get("phase") == "pm")
    ]
    rows = report_module.build_sdk_call_rows(events)
    pm_row = next(r for r in rows if r.agent == "portfolio_manager")
    assert pm_row.stop_reason is None
    assert pm_row.duration_s is None


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_emits_valid_html_with_dark_mode_css(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """The rendered page carries dark-mode CSS + the verdict ribbon."""
    archive_root, invocation_id = _make_archive(
        tmp_path,
        pipeline_log="2026-05-16 INFO scheduler.orchestrator pipeline OK\n",
    )
    archive = report_module.load_invocation_archive(
        archive_root=archive_root, invocation_id=invocation_id
    )

    html = report_module.render(archive)
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html
    # The dark-mode background colour from the sidecar stylesheet leaks
    # into the inline <style> block.
    assert "background" in html or "color-scheme" in html
    # Ribbon contains every in-invocation phase name. The pre-invocation
    # ``seed`` event lives in the sibling ``_pre_invocation`` archive and
    # is intentionally NOT part of the ribbon.
    for phase_name in (
        "phase1",
        "snapshot_assembly",
        "distillation",
        "domain_researchers",
        "qualitative",
        "adaptive",
        "synthesizer",
        "analyst",
        "strategist",
        "pre_processor",
        "pm",
        "phase2",
    ):
        assert phase_name in html
    # Verdict ribbon and SDK-call table land in the body.
    assert "PASS" in html
    assert "portfolio_manager" in html
    # SDK call response fields surface.
    assert "30000" in html or "30,000" in html  # output_tokens for pm
    # InvocationSummary trigger_source visible.
    assert "debug_e2e_cli" in html


def test_render_marks_unfinished_phases_with_fail_badge(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """A pm phase that never emits ``phase_done`` surfaces as FAIL in the ribbon."""
    archive_root, invocation_id = _make_archive(
        tmp_path,
        progress_events=_truncated_progress_stream(),
        pipeline_log="",
    )
    archive = report_module.load_invocation_archive(
        archive_root=archive_root, invocation_id=invocation_id
    )

    html = report_module.render(archive)
    assert "FAIL" in html
    # The failure section names the incomplete phase(s).
    assert "pm" in html
    assert "phase2" in html


def test_render_includes_pipeline_log_excerpt_in_collapsible_details(
    report_module: ModuleType, tmp_path: Path
) -> None:
    """Pipeline log text lands inside a ``<details>`` block so it stays out of the way."""
    log_text = "2026-05-16 INFO scheduler.orchestrator started\n" * 10
    archive_root, invocation_id = _make_archive(tmp_path, pipeline_log=log_text)
    archive = report_module.load_invocation_archive(
        archive_root=archive_root, invocation_id=invocation_id
    )

    html = report_module.render(archive)
    assert "<details>" in html
    assert "scheduler.orchestrator started" in html


def test_render_handles_missing_pipeline_log(report_module: ModuleType, tmp_path: Path) -> None:
    """A missing ``pipeline.log`` doesn't blow up the renderer."""
    archive_root, invocation_id = _make_archive(tmp_path, pipeline_log=None)
    archive = report_module.load_invocation_archive(
        archive_root=archive_root, invocation_id=invocation_id
    )

    html = report_module.render(archive)
    assert "<!DOCTYPE html>" in html
    assert "no pipeline log" in html.lower() or "pipeline.log" in html
