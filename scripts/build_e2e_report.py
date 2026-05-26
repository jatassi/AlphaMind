"""Render an HTML e2e-verification report from a debug-e2e archive.

Consumes the single-invocation archive produced by ``python -m
alphamind.scheduler run --debug-e2e``:

* ``<archive_root>/invocations/<invocation_id>/progress.jsonl`` —
  canonical event log of 12 in-invocation ``phase_start``/``phase_done``
  pairs plus 10 ``agent_request``/``agent_response`` pairs (one per
  Sonnet/Opus agent call); see parent issue ALP-493 §§ (D), (E). The
  pre-invocation ``seed`` event lands in the sibling
  ``<archive_root>/invocations/_pre_invocation/progress.jsonl`` and is
  not consumed by this builder.
* ``<archive_root>/invocations/<invocation_id>/resolved_config.json``
  — the orchestrator's serialized configuration view.
* ``<archive_root>/invocations/<invocation_id>/pipeline.log`` —
  optional plaintext pipeline log emitted by the scheduler-process
  ``TimedRotatingFileHandler`` (Story ALP-431).

Emits one self-contained dark-mode HTML page summarizing the
invocation: a 12-phase verdict ribbon, a 10-row SDK-call table with
each call's ``duration_s`` / ``input_tokens`` / ``cache_read_tokens`` /
``cache_write_tokens`` / ``output_tokens`` / ``tool_calls`` /
``stop_reason``, the resolved-config summary, an incomplete-phase
failure section, and a collapsible pipeline-log excerpt.

Usage::

    uv run python scripts/build_e2e_report.py \\
        --archive-root .archive/verify-debug-e2e \\
        [--invocation-id 20260516T120000Z-debug-e2e] \\
        [--output report.html]

``--invocation-id`` is auto-discovered when exactly one invocation
directory exists under ``<archive-root>/invocations/``.
"""

from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alphamind._kernel.archive_layout import find_invocation_archive_dir
from alphamind.scripts._stdio import configure_utf8_stdio

__all__ = [
    "InvocationArchive",
    "PhaseSummary",
    "SdkCallRow",
    "build_phase_summaries",
    "build_sdk_call_rows",
    "discover_invocation_id",
    "load_invocation_archive",
    "main",
    "render",
]


# ---------------------------------------------------------------------------
# Phase + SDK call vocabulary (mirrors verify_debug_e2e.py)
# ---------------------------------------------------------------------------


# The 12 in-invocation phases per parent issue ALP-493 § (D), in their
# canonical dependency-respecting order. Used for the verdict ribbon's
# deterministic column order — the JSONL itself can interleave the parallel
# pairs. The pre-invocation ``seed`` event lives in the sibling
# ``_pre_invocation`` archive directory and is intentionally NOT part of
# the ribbon.
_PHASE_ORDER: tuple[str, ...] = (
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
)


# ---------------------------------------------------------------------------
# Typed views over the on-disk archive
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvocationArchive:
    """Loaded view of one debug-e2e invocation directory."""

    archive_root: Path
    invocation_id: str
    events: tuple[dict[str, Any], ...]
    resolved_config: dict[str, Any]
    pipeline_log: str


@dataclass(frozen=True, slots=True)
class PhaseSummary:
    """One row of the 13-phase verdict ribbon."""

    name: str
    started: bool
    done: bool
    duration_s: float | None
    verdict: str  # "PASS" | "FAIL"


@dataclass(frozen=True, slots=True)
class SdkCallRow:
    """One row of the SDK-call table — paired ``agent_request``/``response``.

    The three input-side counts mirror the SDK ``usage`` split: most
    AlphaMind prompts hit the cache, so ``cache_read_tokens`` carries the
    bulk of the volume while ``input_tokens`` is the non-cached delta
    (typically single-to-low-double-digit). ``cache_write_tokens`` is the
    cache-creation cost on a miss. Splitting the columns lets the
    operator confirm at a glance that the prompt was assembled (ALP-701).
    """

    phase: str
    agent: str
    model: str
    duration_s: float | None
    input_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    tool_calls: int | None
    stop_reason: str | None


# ---------------------------------------------------------------------------
# Archive discovery + loading
# ---------------------------------------------------------------------------


def discover_invocation_id(archive_root: Path) -> str:
    """Auto-discover the sole invocation directory under ``archive_root``.

    Raises ``ValueError`` when the count is anything other than one, so
    the caller can either fall back to ``--invocation-id`` or surface
    the operator-visible error.

    Searches the date-partitioned canonical layout
    ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/``. Leading-underscore
    directory names (e.g. ``_pre_invocation``) are excluded from the
    candidate set.
    """
    if not archive_root.is_dir():
        msg = f"archive root does not exist: {archive_root}; did the debug-e2e subprocess run?"
        raise ValueError(msg)
    # Collect all invocation dirs across all date partitions.
    candidates = sorted(
        p
        for date_dir in sorted(archive_root.iterdir())
        if date_dir.is_dir() and not date_dir.name.startswith("_")
        for p in date_dir.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )
    if not candidates:
        msg = f"no invocations found under {archive_root}"
        raise ValueError(msg)
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        msg = (
            f"multiple invocations under {archive_root}: {names}; "
            "pass --invocation-id to disambiguate"
        )
        raise ValueError(msg)
    return candidates[0].name


def load_invocation_archive(*, archive_root: Path, invocation_id: str) -> InvocationArchive:
    """Read the three archive files into a typed view.

    ``progress.jsonl`` and ``resolved_config.json`` are required;
    ``pipeline.log`` is optional (the file lives in the scheduler's log
    directory by default, not under the archive — operator copies it in
    when summarizing a run).
    """
    inv_dir = find_invocation_archive_dir(archive_root=archive_root, invocation_id=invocation_id)
    if inv_dir is None or not inv_dir.is_dir():
        msg = f"invocation directory not found for {invocation_id!r} under {archive_root}"
        raise FileNotFoundError(msg)

    events: list[dict[str, Any]] = []
    progress_path = inv_dir / "progress.jsonl"
    if progress_path.is_file():
        with progress_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                events.append(json.loads(line))

    resolved_config: dict[str, Any] = {}
    config_path = inv_dir / "resolved_config.json"
    if config_path.is_file():
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            resolved_config = parsed

    log_path = inv_dir / "pipeline.log"
    pipeline_log = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""

    return InvocationArchive(
        archive_root=archive_root,
        invocation_id=invocation_id,
        events=tuple(events),
        resolved_config=resolved_config,
        pipeline_log=pipeline_log,
    )


# ---------------------------------------------------------------------------
# Phase + SDK-call analyses
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> float | None:
    """Parse an ISO-8601 ``YYYY-MM-DDTHH:MM:SS...`` string into a Unix epoch."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def build_phase_summaries(events: list[dict[str, Any]]) -> list[PhaseSummary]:
    """Collapse the 13 ``phase_start``/``phase_done`` pairs into summary rows.

    The order of the returned list matches ``_PHASE_ORDER`` so the
    verdict ribbon renders deterministically even when the underlying
    JSONL interleaves the two parallel pairs.
    """
    starts: dict[str, str] = {}
    dones: dict[str, str] = {}
    for ev in events:
        kind = ev.get("event")
        phase = ev.get("phase")
        ts = ev.get("timestamp")
        if not isinstance(phase, str) or not isinstance(ts, str):
            continue
        if kind == "phase_start":
            starts.setdefault(phase, ts)
        elif kind == "phase_done":
            dones[phase] = ts

    summaries: list[PhaseSummary] = []
    for name in _PHASE_ORDER:
        started = name in starts
        done = name in dones
        duration: float | None = None
        if started and done:
            start_ts = _parse_iso(starts[name])
            done_ts = _parse_iso(dones[name])
            if start_ts is not None and done_ts is not None:
                duration = done_ts - start_ts
        verdict = "PASS" if (started and done) else "FAIL"
        summaries.append(
            PhaseSummary(
                name=name,
                started=started,
                done=done,
                duration_s=duration,
                verdict=verdict,
            )
        )
    return summaries


def build_sdk_call_rows(events: list[dict[str, Any]]) -> list[SdkCallRow]:
    """Pair every ``agent_request`` with its ``agent_response``.

    Orphan requests (no matching response) render with ``stop_reason``
    ``None`` and ``duration_s`` ``None`` — the operator's signal that
    an SDK call started but never settled.
    """
    requests: dict[tuple[str, str], dict[str, Any]] = {}
    responses: dict[tuple[str, str], dict[str, Any]] = {}
    request_order: list[tuple[str, str]] = []
    for ev in events:
        kind = ev.get("event")
        phase_raw = ev.get("phase")
        agent_raw = ev.get("agent")
        if not isinstance(phase_raw, str) or not isinstance(agent_raw, str):
            continue
        key = (phase_raw, agent_raw)
        if kind == "agent_request":
            if key not in requests:
                requests[key] = ev
                request_order.append(key)
        elif kind == "agent_response":
            responses[key] = ev

    rows: list[SdkCallRow] = []
    for key in request_order:
        req = requests[key]
        resp = responses.get(key)
        model_raw = req.get("model")
        model: str = model_raw if isinstance(model_raw, str) else ""
        if resp is None:
            rows.append(
                SdkCallRow(
                    phase=key[0],
                    agent=key[1],
                    model=model,
                    duration_s=None,
                    input_tokens=None,
                    cache_read_tokens=None,
                    cache_write_tokens=None,
                    output_tokens=None,
                    tool_calls=None,
                    stop_reason=None,
                )
            )
            continue
        duration = resp.get("duration_s")
        rows.append(
            SdkCallRow(
                phase=key[0],
                agent=key[1],
                model=model,
                duration_s=float(duration) if isinstance(duration, (int, float)) else None,
                input_tokens=resp.get("input_tokens")
                if isinstance(resp.get("input_tokens"), int)
                else None,
                cache_read_tokens=resp.get("cache_read_tokens")
                if isinstance(resp.get("cache_read_tokens"), int)
                else None,
                cache_write_tokens=resp.get("cache_write_tokens")
                if isinstance(resp.get("cache_write_tokens"), int)
                else None,
                output_tokens=resp.get("output_tokens")
                if isinstance(resp.get("output_tokens"), int)
                else None,
                tool_calls=resp.get("tool_calls")
                if isinstance(resp.get("tool_calls"), int)
                else None,
                stop_reason=resp.get("stop_reason")
                if isinstance(resp.get("stop_reason"), str)
                else None,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# HTML rendering primitives — preserved from the legacy script
# ---------------------------------------------------------------------------


def _esc(s: object) -> str:
    return html.escape(str(s)) if s is not None else ""


def _verdict_badge(verdict: str) -> str:
    cls = {"PASS": "pass", "WARN": "warn", "FAIL": "fail", "SKIP": "skip"}.get(verdict, "info")
    return f'<span class="badge {cls}">{_esc(verdict)}</span>'


def _stat_table(rows: list[tuple[str, str]]) -> str:
    body = "\n".join(f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>" for k, v in rows)
    return f'<table class="stats">{body}</table>'


def _details(summary: str, body: str, *, open_: bool = False) -> str:
    o = " open" if open_ else ""
    return (
        f"<details{o}><summary>{_esc(summary)}</summary>\n"
        f'<div class="details-body">{body}</div>\n'
        f"</details>"
    )


def _pre(text: str) -> str:
    return f"<pre>{_esc(text)}</pre>"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n…[truncated]"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _phase_ribbon(summaries: list[PhaseSummary]) -> str:
    """13-column phase verdict ribbon — one ``<div>`` per phase, colour-coded."""
    cards: list[str] = []
    for s in summaries:
        cls = "pass" if s.verdict == "PASS" else "fail"
        duration_txt = f"{s.duration_s:.1f}s" if s.duration_s is not None else "—"
        cards.append(
            f'<div class="ribbon-card {cls}">'
            f'<span class="ribbon-name">{_esc(s.name)}</span>'
            f'<span class="ribbon-verdict">{_esc(s.verdict)}</span>'
            f'<span class="ribbon-duration">{_esc(duration_txt)}</span>'
            f"</div>"
        )
    return f'<div class="phase-ribbon">{"".join(cards)}</div>'


def _format_tokens(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,}"


def _sdk_call_table(rows: list[SdkCallRow]) -> str:
    """One row per SDK call with ``agent_response`` metrics surfaced as columns."""
    if not rows:
        return '<p class="empty">No SDK calls recorded.</p>'
    tr_lines: list[str] = []
    for r in rows:
        duration_txt = f"{r.duration_s:.2f}s" if r.duration_s is not None else "—"
        stop = r.stop_reason if r.stop_reason is not None else "<em>pending</em>"
        status_cls = "pass" if r.stop_reason is not None else "warn"
        tr_lines.append(
            "<tr>"
            f"<td>{_esc(r.phase)}</td>"
            f"<td>{_esc(r.agent)}</td>"
            f"<td>{_esc(r.model)}</td>"
            f"<td>{_esc(duration_txt)}</td>"
            f'<td class="num">{_esc(_format_tokens(r.input_tokens))}</td>'
            f'<td class="num">{_esc(_format_tokens(r.cache_read_tokens))}</td>'
            f'<td class="num">{_esc(_format_tokens(r.cache_write_tokens))}</td>'
            f'<td class="num">{_esc(_format_tokens(r.output_tokens))}</td>'
            f'<td class="num">{_esc(_format_tokens(r.tool_calls))}</td>'
            f'<td class="status {status_cls}">{stop}</td>'
            "</tr>"
        )
    return (
        '<table class="stats wide">'
        "<thead><tr>"
        "<th>Phase</th><th>Agent</th><th>Model</th><th>Duration</th>"
        "<th>Input tokens</th><th>Cache read</th><th>Cache write</th>"
        "<th>Output tokens</th><th>Tool calls</th>"
        "<th>Stop reason</th>"
        "</tr></thead>"
        f"<tbody>{''.join(tr_lines)}</tbody>"
        "</table>"
    )


def _invocation_summary_section(archive: InvocationArchive) -> str:
    cfg = archive.resolved_config
    rows: list[tuple[str, str]] = [
        ("invocation_id", archive.invocation_id),
        ("trigger_source", str(cfg.get("trigger_source", "—"))),
        ("firing_run_type", str(cfg.get("firing_run_type", "—"))),
        ("active_profile", str(cfg.get("active_profile", "—"))),
        ("active_regime", str(cfg.get("active_regime", "—"))),
        ("active_mode", str(cfg.get("active_mode", "—"))),
    ]
    return (
        '<section class="phase" id="invocation-summary">'
        "<h2>Invocation summary</h2>"
        f"{_stat_table(rows)}"
        "</section>"
    )


def _failure_section(summaries: list[PhaseSummary]) -> str:
    incomplete = [s for s in summaries if s.verdict == "FAIL"]
    if not incomplete:
        return ""
    items = "".join(
        f"<li><code>{_esc(s.name)}</code>: "
        f"{'no phase_done emitted' if s.started else 'phase never started'}"
        "</li>"
        for s in incomplete
    )
    return (
        '<section class="phase failure" id="incomplete-phases">'
        f"<h2>Incomplete phases {_verdict_badge('FAIL')}</h2>"
        f'<ul class="failure-list">{items}</ul>'
        "</section>"
    )


def _pipeline_log_section(archive: InvocationArchive) -> str:
    if not archive.pipeline_log:
        return (
            '<section class="phase" id="pipeline-log">'
            "<h2>Pipeline log</h2>"
            '<p class="empty">No <code>pipeline.log</code> in archive — '
            "copy it from <code>~/AlphaMind/logs/pipeline.log</code> "
            "if you want the log in the report.</p>"
            "</section>"
        )
    excerpt = _details(
        "pipeline.log excerpt (last 8 KB)",
        _pre(_truncate(archive.pipeline_log, 8000)),
    )
    return f'<section class="phase" id="pipeline-log"><h2>Pipeline log</h2>{excerpt}</section>'


def _resolved_config_section(archive: InvocationArchive) -> str:
    """Verbatim ``resolved_config.json`` for cross-referencing."""
    body = json.dumps(archive.resolved_config, indent=2, sort_keys=True)
    return (
        '<section class="phase" id="resolved-config">'
        "<h2>Resolved config</h2>"
        f"{_details('resolved_config.json (full payload)', _pre(_truncate(body, 16000)))}"
        "</section>"
    )


# ---------------------------------------------------------------------------
# HTML scaffold + CSS load
# ---------------------------------------------------------------------------


_CSS_PATH = Path(__file__).resolve().parent / "_e2e_report_assets" / "style.css"


def _load_css() -> str:
    return _CSS_PATH.read_text(encoding="utf-8")


def render(archive: InvocationArchive) -> str:
    """Return one self-contained HTML page summarizing the debug-e2e archive."""
    events = list(archive.events)
    summaries = build_phase_summaries(events)
    sdk_rows = build_sdk_call_rows(events)
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    css = _load_css()

    body_sections = [
        _invocation_summary_section(archive),
        f'<section class="phase" id="phase-ribbon"><h2>Phase verdict ribbon</h2>'
        f"{_phase_ribbon(summaries)}</section>",
        _failure_section(summaries),
        '<section class="phase" id="sdk-calls"><h2>SDK call summary</h2>'
        f"{_sdk_call_table(sdk_rows)}</section>",
        _pipeline_log_section(archive),
        _resolved_config_section(archive),
    ]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaMind — Debug-E2E Verification ({_esc(archive.invocation_id)})</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>AlphaMind Debug-E2E Verification</h1>
  <div class="meta">
    Report generated: {now_iso}<br>
    Archive root: <code>{_esc(str(archive.archive_root))}</code> ·
    Invocation: <code>{_esc(archive.invocation_id)}</code>
  </div>
</header>
<main>
{"".join(body_sections)}
</main>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="build_e2e_report",
        description=("Render an HTML e2e-verification report from a debug-e2e archive."),
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        required=True,
        help="Path to the verification archive root (e.g. .archive/verify-debug-e2e).",
    )
    parser.add_argument(
        "--invocation-id",
        default=None,
        help=(
            "Invocation id under ``<archive-root>/invocations/`` "
            "(auto-discovered when exactly one invocation directory exists)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output HTML path. Defaults to <archive-root>/debug-e2e-report.html.",
    )
    args = parser.parse_args(argv)

    archive_root = args.archive_root.resolve()
    if not archive_root.exists():
        parser.error(f"archive_root does not exist: {archive_root}")

    invocation_id = args.invocation_id
    if invocation_id is None:
        try:
            invocation_id = discover_invocation_id(archive_root)
        except ValueError as exc:
            parser.error(str(exc))

    archive = load_invocation_archive(archive_root=archive_root, invocation_id=invocation_id)

    output_path = (args.output or (archive_root / "debug-e2e-report.html")).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render(archive), encoding="utf-8")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
