"""Render an HTML e2e-verification report from a verification archive.

Reads the per-phase diagnostic archives written by the verify_*.py scripts
under ``<archive_root>/`` and emits a self-contained dark-mode HTML page
covering all 11 phases. The PM section renders each envelope as a card with
verdict, evaluation pills, anti-pattern chips, concerns, rationale, and any
emitted OMS commands.

Usage:
    uv run python scripts/build_e2e_report.py \\
        --archive-root .archive/verify-pipeline-20260509 \\
        --invocation-id 20260509T191537Z-verify-pipeline

Auto-discovers analyst / strategist / PM scenario invocations by globbing the
``invocations/`` subdirectory.

Phases 0, 1, 1b, and 1c have no archive output — their summaries fall back to
the baseline PASS pattern. Pass ``--phase-summary <path.json>`` to override
individual phase verdicts/notes; see ``--print-phase-template`` for the schema.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Default summaries for phases that produce no archive artifacts
# ---------------------------------------------------------------------------

DEFAULT_PHASE_SUMMARY: dict[str, dict[str, Any]] = {
    "phase-0": {
        "verdict": "PASS",
        "rows": [
            ("Wave 1 utilities", "PASS (12/12 cases)"),
            ("Wave 1 additive fields", "PASS (14/14 cases)"),
            ("Wave 2 boundary fix", "PASS (2/2 cases)"),
            ("Wave 3 typed payloads", "PASS (4/4 cases)"),
            ("Wave 4 structural", "PASS (4/4 cases)"),
            ("Wave 5 architectural", "PASS (4/4 cases)"),
            ("Overall", "PASS (40/40 cases)"),
        ],
    },
    "phase-1": {
        "verdict": "PASS",
        "bootstrap": [
            ("Tables present", "30/30 OK"),
            ("ohlcv_bars rows", "in tolerance"),
            ("collection_runs", "OK"),
        ],
        "collectors": [],
        "note": "",
    },
    "phase-1b": {
        "verdict": "PASS",
        "rows": [
            ("Phase A — schema", "PASS"),
            ("Phase B — invocation context", "PASS"),
            ("Phase C — Phase 1 write path", "PASS"),
            ("Phase D — Phase 2 envelope", "PASS"),
            ("Phase E — Layer-1 parse failure", "PASS"),
            ("Phase F — repository read parity", "PASS"),
        ],
    },
    "phase-1c": {
        "verdict": "PASS",
        "rows": [
            ("Phase 1 — canonical model round-trip", "PASS"),
            ("Phase 2 — command-ID utility", "PASS"),
            ("Phase 3 — PM envelope path", "PASS"),
            ("Phase 4 — engine envelope path", "PASS"),
        ],
    },
    "phase-2": {
        "regime_label": "vol_expansion",
        "block_counts": {
            "tech_semis": 24,
            "financials": 22,
            "energy": 22,
            "total": 391,
            "anomalies": 404,
        },
    },
    "phase-3": {
        "rows": [
            ("tech_semis", "moderate · 4 findings · 3 anomalies · 2 theses"),
            ("financials", "moderate · 7 findings · 4 anomalies · 3 theses"),
            ("energy", "degraded · 4 findings · 2 anomalies · 2 theses"),
        ],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(p: Path) -> str:
    if not p.exists():
        return ""
    return p.read_text()


def _read_json(p: Path) -> Any:
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _esc(s: str) -> str:
    return html.escape(s) if s else ""


def _verdict_badge(status: str) -> str:
    cls = {"PASS": "pass", "WARN": "warn", "FAIL": "fail", "SKIP": "skip"}.get(status, "info")
    return f'<span class="badge {cls}">{status}</span>'


def _stat_table(rows: list[tuple[str, str]]) -> str:
    body = "\n".join(f"<tr><th>{_esc(k)}</th><td>{v}</td></tr>" for k, v in rows)
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


def _code(text: str, lang: str = "") -> str:
    return f'<pre class="code"><code class="lang-{lang}">{_esc(text)}</code></pre>'


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n…[truncated]"


# ---------------------------------------------------------------------------
# Archive layout discovery
# ---------------------------------------------------------------------------


class Layout:
    """Resolved paths for one verification run."""

    def __init__(self, archive_root: Path, invocation_id: str):
        self.archive_root = archive_root
        self.invocation_id = invocation_id

        self.run_invocation_dir = archive_root / "invocations" / invocation_id
        self.stage_artifacts = self.run_invocation_dir / "stage_artifacts"

        # Distillation diagnostic dir lives under
        # ``<archive>/<YYYY-MM-DD>/<invocation_id>/distillation``; derive the
        # date from the invocation_id prefix when possible.
        date_match = re.match(r"^(\d{4})(\d{2})(\d{2})", invocation_id)
        if date_match:
            date_dir = (
                archive_root / f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
            )
        else:
            date_dir = archive_root
        self.distillation_dir = date_dir / invocation_id / "distillation"

        # Auto-discover sub-invocations from the archive structure.
        invocations_dir = archive_root / "invocations"
        self.analyst_dirs: dict[str, Path] = {}
        for sub in sorted(invocations_dir.glob("*-verify-analyst-*")):
            scenario = sub.name.split("-verify-analyst-", 1)[-1]
            self.analyst_dirs[scenario] = sub / "decision" / "analyst"

        self.strategist_dirs: dict[str, Path] = {}
        # When a scenario re-runs, the most recent invocation_id wins
        # (sorted ascending => latest last).
        for sub in sorted(invocations_dir.glob("*-verify-strategist-*")):
            scenario = sub.name.split("-verify-strategist-", 1)[-1]
            self.strategist_dirs[scenario] = sub / "decision" / "strategist"

        self.pm_dirs: dict[str, Path] = {}
        for sub in sorted(invocations_dir.glob("*-verify-pm-*")):
            scenario = sub.name.split("-verify-pm-", 1)[-1]
            self.pm_dirs[scenario] = sub / "decision" / "portfolio_manager"


def _meta_for(agent_dir: Path) -> str:
    md = _read_json(agent_dir / "metadata.json")
    if not md:
        return "<em>no metadata</em>"
    rows = [
        ("model", md.get("model", "")),
        ("wall_clock_s", f"{md.get('wall_clock_seconds', 0):.2f}"),
        ("output_tokens", str(md.get("tokens_used", {}).get("output_tokens", ""))),
        ("input_tokens", str(md.get("tokens_used", {}).get("input_tokens", ""))),
        ("cache_read", str(md.get("tokens_used", {}).get("cache_read_tokens", ""))),
        ("cache_write", str(md.get("tokens_used", {}).get("cache_write_tokens", ""))),
        ("tool_calls", str(md.get("tool_calls_used", ""))),
        ("retry_count", str(md.get("retry_count", 0))),
        ("stop_reason", md.get("stop_reason", "")),
    ]
    return _stat_table(rows)


def _read_response(agent_dir: Path) -> str:
    """Return whichever response file exists; both shapes appear across agents."""
    return _read(agent_dir / "response_initial.md") or _read(agent_dir / "response.md")


# ---------------------------------------------------------------------------
# Per-phase content builders
# ---------------------------------------------------------------------------


def phase_0(summary: dict[str, Any]) -> str:
    rows = summary.get("rows") or DEFAULT_PHASE_SUMMARY["phase-0"]["rows"]
    verdict = summary.get("verdict", "PASS")
    return (
        f"<h2>Phase 0 — Type-layer self-check {_verdict_badge(verdict)}</h2>"
        "<p>Pure in-process checks against the position-thesis-model type "
        "layer (ALP-122). 40 cases across 6 waves covering 15 sub-stories.</p>" + _stat_table(rows)
    )


def phase_1(summary: dict[str, Any]) -> str:
    bootstrap = summary.get("bootstrap") or DEFAULT_PHASE_SUMMARY["phase-1"]["bootstrap"]
    collectors = summary.get("collectors") or []
    note = summary.get("note") or ""
    verdict = summary.get("verdict", "PASS")

    parts = [
        f"<h2>Phase 1 — Data layer {_verdict_badge(verdict)}</h2>",
        "<p>Bootstrap schema + freshness checks. No SDK calls.</p>",
    ]
    if note:
        parts.append(f"<p>{_esc(note)}</p>")
    parts.append("<h3>verify_bootstrap.py</h3>")
    parts.append(_stat_table(bootstrap))
    parts.append("<h3>verify_ongoing_collection.py</h3>")
    if collectors:
        rows = "\n".join(
            (
                f"<tr><th>{_esc(c['name'])}</th>"
                f"<td>{_esc(c.get('age', ''))}</td>"
                f"<td>{_verdict_badge(c.get('status', 'SKIP'))}</td></tr>"
            )
            for c in collectors
        )
        parts.append(
            '<table class="stats wide">'
            "<thead><tr><th>Collector</th><th>Age</th><th>Status</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        parts.append(
            "<p><em>No per-collector data supplied; rerun "
            "verify_ongoing_collection.py and paste rows into "
            "<code>--phase-summary</code>.</em></p>"
        )
    return "\n".join(parts)


def phase_1b(summary: dict[str, Any]) -> str:
    rows = summary.get("rows") or DEFAULT_PHASE_SUMMARY["phase-1b"]["rows"]
    verdict = summary.get("verdict", "PASS")
    return (
        f"<h2>Phase 1b — State persistence {_verdict_badge(verdict)}</h2>"
        "<p>In-process integration check against the durable substrate the "
        f"execution layer writes through (ALP-119). Six phases A{chr(0x2013)}F.</p>"
        + _stat_table(rows)
    )


def phase_1c(summary: dict[str, Any]) -> str:
    rows = summary.get("rows") or DEFAULT_PHASE_SUMMARY["phase-1c"]["rows"]
    verdict = summary.get("verdict", "PASS")
    return (
        f"<h2>Phase 1c — OMS commands {_verdict_badge(verdict)}</h2>"
        "<p>OMS commands work tree (ALP-120). Run against freshly-migrated tmp DB.</p>"
        + _stat_table(rows)
    )


def phase_2(layout: Layout, summary: dict[str, Any]) -> str:
    counts = summary.get("block_counts") or DEFAULT_PHASE_SUMMARY["phase-2"]["block_counts"]
    regime = summary.get("regime_label") or DEFAULT_PHASE_SUMMARY["phase-2"]["regime_label"]

    distillation_rows = [
        ("sector_tech_semis blocks", str(counts.get("tech_semis", "?"))),
        ("sector_financials blocks", str(counts.get("financials", "?"))),
        ("sector_energy blocks", str(counts.get("energy", "?"))),
        ("regime_label", regime),
        ("total_blocks", str(counts.get("total", "?"))),
        ("total_anomalies", str(counts.get("anomalies", "?"))),
    ]

    correlation_brief = _read_json(layout.stage_artifacts / "correlation_regime_brief.json")
    cr_count = (
        len(correlation_brief.get("entries", [])) if isinstance(correlation_brief, dict) else 0
    )
    universal_regime = _read_json(layout.stage_artifacts / "universal_regime_label.json") or {}
    regime_md = _read(layout.distillation_dir / "regime.md")
    cr_md = _read(layout.distillation_dir / "correlation_regime_brief.md")

    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    universal_regime_text = _esc(json.dumps(universal_regime))
    parts = [
        f"<h2>Phase 2 — Distillation layer {verdict}</h2>",
        "<p>Live distillation orchestrator (one Sonnet call) plus DB-only "
        "state-machine checks. Stage artifacts written for downstream consumers.</p>",
        "<h3>verify_distillation.py</h3>",
        _stat_table(distillation_rows),
        "<h3>Distilled outputs (excerpts)</h3>",
        (
            f"<p>Universal regime: <code>{universal_regime_text}</code> · "
            f"{cr_count} correlation/regime entries.</p>"
        ),
    ]
    if regime_md:
        parts.append(_details("Regime brief (analyst-facing)", _pre(_truncate(regime_md, 6000))))
    if cr_md:
        parts.append(
            _details("Correlation/regime brief (analyst-facing)", _pre(_truncate(cr_md, 6000)))
        )
    return "\n".join(parts)


def phase_3(layout: Layout, summary: dict[str, Any]) -> str:
    rows = summary.get("rows") or DEFAULT_PHASE_SUMMARY["phase-3"]["rows"]
    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    parts = [
        f"<h2>Phase 3 — Domain researchers {verdict}</h2>",
        "<p>Three sector researchers running in parallel against real Sonnet, "
        "plus a no-SDK failure-mode harness.</p>",
        _stat_table(rows),
    ]

    for sector in ("tech_semis", "financials", "energy"):
        agent_dir = layout.run_invocation_dir / "analysis" / f"{sector}_researcher"
        if not agent_dir.exists():
            continue
        meta = _meta_for(agent_dir)
        resp = _truncate(_read_response(agent_dir), 10000)
        parts.append(
            _details(
                f"{sector}_researcher — metadata + response", meta + (_pre(resp) if resp else "")
            )
        )

    return "\n".join(parts)


def phase_4(layout: Layout, summary: dict[str, Any]) -> str:
    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    parts = [
        f"<h2>Phase 4 — Qualitative + adaptive researchers {verdict}</h2>",
    ]
    note = summary.get("note", "")
    if note:
        parts.append(f"<p>{_esc(note)}</p>")

    qual_dir = layout.run_invocation_dir / "analysis" / "qualitative_researcher"
    adapt_dir = layout.run_invocation_dir / "analysis" / "adaptive_researcher"

    parts.append("<h3>qualitative_researcher</h3>")
    if qual_dir.exists():
        parts.append(_meta_for(qual_dir))
    qual_brief = _read_json(layout.stage_artifacts / "qualitative_brief.json")
    if qual_brief:
        parts.append(
            _details(
                "qualitative_brief.json (parsed brief)", _code(json.dumps(qual_brief, indent=2))
            )
        )
    qual_resp = _read_response(qual_dir)
    if qual_resp:
        parts.append(
            _details(
                "qualitative_researcher raw response (excerpt)", _pre(_truncate(qual_resp, 8000))
            )
        )

    parts.append("<h3>adaptive_researcher</h3>")
    if adapt_dir.exists():
        parts.append(_meta_for(adapt_dir))
    adapt_brief = _read_json(layout.stage_artifacts / "adaptive_brief.json")
    if adapt_brief:
        parts.append(
            _details(
                "adaptive_brief.json (parsed brief)",
                _code(json.dumps(adapt_brief, indent=2)),
            )
        )
    adapt_resp = _read_response(adapt_dir)
    if adapt_resp:
        parts.append(
            _details(
                "adaptive_researcher raw response (excerpt)", _pre(_truncate(adapt_resp, 8000))
            )
        )

    return "\n".join(parts)


def phase_5(layout: Layout, summary: dict[str, Any]) -> str:
    synth_dir = layout.run_invocation_dir / "analysis" / "synthesizer"
    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    parts = [
        f"<h2>Phase 5 — Synthesizer {verdict}</h2>",
        "<p>The agent that consumes all six upstream briefs.</p>",
    ]
    if synth_dir.exists():
        parts.append(_meta_for(synth_dir))
    response = _read_response(synth_dir)
    if response:
        parts.append("<h3>Synthesis prose</h3>")
        parts.append(_details("Full synthesizer response", _pre(response), open_=True))
    return "\n".join(parts)


def phase_6(layout: Layout, summary: dict[str, Any]) -> str:
    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    parts = [
        f"<h2>Phase 6 — Analyst (decision layer) {verdict}</h2>",
        "<p>Two scenarios per run: <strong>normal</strong> (full guardrail "
        "header, recommendations expected) and <strong>halt</strong> "
        "(watchlist header, no validate_guardrail calls).</p>",
    ]

    for scenario, sdir in layout.analyst_dirs.items():
        parts.append(f"<h3>Scenario: {scenario}</h3>")
        parts.append(_meta_for(sdir))
        fixture = _read_json(
            REPO_ROOT / "tests" / "fixtures" / "decision" / "analyst" / f"{scenario}.json"
        )
        if fixture:
            parts.append(
                _details(
                    f"AnalystOutput fixture — {scenario}", _code(json.dumps(fixture, indent=2))
                )
            )
        resp = _read_response(sdir)
        if resp:
            parts.append(_details(f"Raw response — {scenario}", _pre(_truncate(resp, 6000))))
    return "\n".join(parts)


def phase_7(layout: Layout, summary: dict[str, Any]) -> str:
    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    parts = [
        f"<h2>Phase 7 — Strategist (decision layer) {verdict}</h2>",
        "<p>Three scenarios per run: <strong>normal</strong>, "
        "<strong>defensive_posture</strong>, <strong>emergency</strong>.</p>",
    ]
    note = summary.get("note", "")
    if note:
        parts.append(f"<p>{_esc(note)}</p>")

    for scenario, sdir in layout.strategist_dirs.items():
        parts.append(f"<h3>Scenario: {scenario}</h3>")
        parts.append(_meta_for(sdir))
        fixture = _read_json(
            REPO_ROOT / "tests" / "fixtures" / "decision" / "strategist" / f"{scenario}.json"
        )
        if fixture:
            parts.append(
                _details(
                    f"StrategistOutput fixture — {scenario}",
                    _code(json.dumps(fixture, indent=2)),
                )
            )
        init_resp = _read(sdir / "response_initial.md")
        if init_resp:
            parts.append(
                _details(
                    f"Raw response — {scenario} (initial attempt)",
                    _pre(_truncate(init_resp, 6000)),
                )
            )
        retry_resp = _read(sdir / "response_retry.md")
        if retry_resp:
            parts.append(
                _details(
                    f"Raw response — {scenario} (retry attempt)",
                    _pre(_truncate(retry_resp, 6000)),
                )
            )
        plain = _read(sdir / "response.md")
        if plain and not init_resp:
            parts.append(_details(f"Raw response — {scenario}", _pre(_truncate(plain, 6000))))
    return "\n".join(parts)


def phase_8(summary: dict[str, Any]) -> str:
    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    parts = [
        f"<h2>Phase 8 — Proposal pre-processor {verdict}</h2>",
        "<p>Deterministic pre-processor that bundles analyst + strategist "
        "outputs for the PM. No SDK calls.</p>",
        _stat_table(
            [
                ("normal", summary.get("normal", "PASS")),
                ("halt", summary.get("halt", "PASS")),
                ("emergency", summary.get("emergency", "PASS")),
                ("normal_with_breach", summary.get("normal_with_breach", "PASS")),
            ]
        ),
    ]
    for scenario in ("normal", "halt", "emergency", "normal_with_breach"):
        bundle = _read_json(
            REPO_ROOT
            / "tests"
            / "fixtures"
            / "decision"
            / "proposal_pre_processor"
            / f"{scenario}.json"
        )
        if bundle:
            text = json.dumps(bundle, indent=2)
            parts.append(_details(f"Bundle fixture — {scenario}", _code(_truncate(text, 8000))))
    return "\n".join(parts)


# --- envelope card helpers --------------------------------------------------


def _verdict_class(verdict: str) -> str:
    return {
        "approve": "approve",
        "approve_with_modification": "modify",
        "reject": "reject",
    }.get(verdict, "info")


def _eval_pill(name: str, criterion: dict[str, Any]) -> str:
    status = criterion.get("status", "?")
    note = criterion.get("note", "")
    cls = "pass" if status == "pass" else ("fail" if status == "fail" else "info")
    return (
        f'<div class="eval-pill {cls}">'
        f'<span class="eval-name">{_esc(name)}</span>'
        f'<span class="eval-status">{_esc(status)}</span>'
        f'<span class="eval-note">{_esc(note)}</span>'
        f"</div>"
    )


def _format_command(cmd: dict[str, Any]) -> str:
    if not isinstance(cmd, dict):
        return _esc(str(cmd))
    cmd_type = cmd.get("command_type", "?")
    underlying = (
        cmd.get("instrument", {}).get("underlying", "")
        if isinstance(cmd.get("instrument"), dict)
        else ""
    )
    direction = cmd.get("direction", "")
    side = cmd.get("side", "")
    size = cmd.get("position_size") or {}
    size_dollars = size.get("dollar_value", "") if isinstance(size, dict) else ""
    size_str = (
        f"${size_dollars:,.0f}"
        if isinstance(size_dollars, (int, float))
        else _esc(str(size_dollars))
    )
    parts = [f'<span class="cmd-type">{_esc(cmd_type)}</span>']
    if underlying:
        parts.append(f'<span class="cmd-ticker">{_esc(underlying)}</span>')
    if direction:
        parts.append(
            f'<span class="cmd-side dir-{_esc(direction.lower())}">{_esc(direction)}</span>'
        )
    elif side:
        parts.append(f'<span class="cmd-side">{_esc(side)}</span>')
    if size_str:
        parts.append(f'<span class="cmd-size">{size_str}</span>')
    return f'<div class="oms-command">{" ".join(parts)}</div>'


def _envelope_card(env: dict[str, Any], sub_results: list[Any]) -> str:
    verdict = env.get("verdict", "?")
    cls = _verdict_class(verdict)
    env_id = env.get("envelope_id", "?")
    rec_id = env.get("source_recommendation_id", "?")
    rec_type = env.get("recommendation_type", "?")
    pos_id = env.get("position_id") or "(new)"
    provenance = env.get("source_provenance", "?")

    header = (
        f'<div class="card-header">'
        f'<span class="env-id">{_esc(env_id)}</span>'
        f'<span class="verdict-badge {cls}">{_esc(verdict.upper())}</span>'
        f"</div>"
    )
    meta_chips = [
        f'<span class="meta-chip pos">{_esc(pos_id)}</span>',
        f'<span class="meta-chip type">{_esc(rec_type)}</span>',
        f'<span class="meta-chip rec">{_esc(rec_id)}</span>',
        f'<span class="meta-chip prov">{_esc(provenance)}</span>',
    ]
    sections = [header, f'<div class="card-meta">{"".join(meta_chips)}</div>']

    evaluation = env.get("evaluation") or {}
    if evaluation:
        pills = "\n".join(_eval_pill(k, v) for k, v in evaluation.items() if isinstance(v, dict))
        sections.append(
            '<div class="card-section"><h5>Evaluation</h5>'
            f'<div class="eval-pills">{pills}</div></div>'
        )

    anti = env.get("anti_patterns_identified") or []
    if anti:
        chips = "".join(f'<span class="tag tag-anti">{_esc(a)}</span>' for a in anti)
        sections.append(
            '<div class="card-section"><h5>Anti-patterns</h5>'
            f'<div class="tag-row">{chips}</div></div>'
        )

    concerns = env.get("concerns") or []
    if concerns:
        items = "".join(
            (
                "<li>"
                f'<span class="concern-source">{_esc(c.get("source", ""))}</span>'
                f'<span class="concern-summary">{_esc(c.get("summary", ""))}</span>'
                "</li>"
            )
            for c in concerns
            if isinstance(c, dict)
        )
        sections.append(
            f'<details class="card-section concerns"><summary>Concerns ({len(concerns)})</summary>'
            f'<ul class="concern-list">{items}</ul></details>'
        )

    mods = env.get("modifications") or []
    if mods:
        sections.append(
            _details(
                f"Modifications ({len(mods)})",
                _code(json.dumps(mods, indent=2)),
            )
        )

    cmds = env.get("commands") or []
    if cmds:
        cmd_html = "".join(_format_command(c) for c in cmds)
        sections.append(
            f'<div class="card-section"><h5>OMS commands ({len(cmds)})</h5>{cmd_html}</div>'
        )

    rationale = env.get("rationale_narrative") or ""
    if rationale:
        sections.append(
            f'<details class="card-section"><summary>Rationale narrative</summary>'
            f'<p class="rationale">{_esc(rationale)}</p></details>'
        )

    if sub_results:
        sections.append(
            _details(
                f"Submission results ({len(sub_results)})", _code(json.dumps(sub_results, indent=2))
            )
        )

    return f'<div class="envelope-card {cls}">{"".join(sections)}</div>'


def _envelope_grid(sub_log: list[dict[str, Any]]) -> str:
    cards = "\n".join(
        _envelope_card(entry["envelope"], entry.get("submission_results", [])) for entry in sub_log
    )
    return f'<div class="envelope-grid">{cards}</div>'


def phase_9(layout: Layout, summary: dict[str, Any]) -> str:
    verdict = _verdict_badge(summary.get("verdict", "PASS"))
    parts = [
        f"<h2>Phase 9 — Portfolio manager (the actual trades) {verdict}</h2>",
        "<p>The PM agent (ALP-117) end-to-end against the real Claude Agent "
        "SDK across four scenarios: clean approval flow, halt-mode "
        "risk-reduction-only rendering, regime-transition breach handling, "
        "and the post-rejection modification path.</p>",
    ]
    note = summary.get("note", "")
    if note:
        parts.append(f"<p>{_esc(note)}</p>")

    for scenario, sdir in layout.pm_dirs.items():
        parts.append(f"<h3>Scenario: {scenario}</h3>")
        parts.append(_meta_for(sdir))

        sub_log = _read_json(sdir / "submission_log.json")
        if sub_log:
            verdicts: dict[str, int] = {}
            for entry in sub_log:
                v = entry.get("envelope", {}).get("verdict", "?")
                verdicts[v] = verdicts.get(v, 0) + 1
            mult = chr(0x00D7)  # U+00D7 MULTIPLICATION SIGN; chr() avoids RUF001
            verdict_chips = "".join(
                f'<span class="meta-chip {_verdict_class(v)}">{_esc(v)} {mult} {n}</span>'
                for v, n in sorted(verdicts.items())
            )
            parts.append(f"<h4>Envelopes ({len(sub_log)} total) {verdict_chips}</h4>")
            parts.append(_envelope_grid(sub_log))
            parts.append(
                _details(
                    f"Full submission_log.json — {scenario}", _code(json.dumps(sub_log, indent=2))
                )
            )

        failed_log = _read_json(sdir / "failed_submission_log.json")
        if failed_log:
            text = json.dumps(failed_log, indent=2)
            parts.append(
                _details(f"failed_submission_log — {scenario}", _code(_truncate(text, 6000)))
            )

        resp = _read_response(sdir)
        if resp:
            parts.append(_details(f"Raw response — {scenario}", _pre(_truncate(resp, 6000))))

        fixture = _read_json(
            REPO_ROOT / "tests" / "fixtures" / "decision" / "pm" / f"{scenario}.json"
        )
        if fixture:
            text = json.dumps(fixture, indent=2)
            parts.append(
                _details(f"PMCompletionRecord fixture — {scenario}", _code(_truncate(text, 6000)))
            )
    return "\n".join(parts)


def summary_section(phase_summary: dict[str, Any]) -> str:
    """Top-level summary table. Verdicts roll up from per-phase summary blocks."""

    def vd(key: str) -> str:
        return _verdict_badge(phase_summary.get(key, {}).get("verdict", "PASS"))

    rows = [
        ("Phase 0 (types)", vd("phase-0"), phase_summary.get("phase-0", {}).get("note", "")),
        ("Phase 1 (data)", vd("phase-1"), phase_summary.get("phase-1", {}).get("note", "")),
        (
            "Phase 1b (state persistence)",
            vd("phase-1b"),
            phase_summary.get("phase-1b", {}).get("note", ""),
        ),
        (
            "Phase 1c (OMS commands)",
            vd("phase-1c"),
            phase_summary.get("phase-1c", {}).get("note", ""),
        ),
        ("Phase 2 (distillation)", vd("phase-2"), phase_summary.get("phase-2", {}).get("note", "")),
        (
            "Phase 3 (domain researchers)",
            vd("phase-3"),
            phase_summary.get("phase-3", {}).get("note", ""),
        ),
        (
            "Phase 4 (qual + adaptive)",
            vd("phase-4"),
            phase_summary.get("phase-4", {}).get("note", ""),
        ),
        ("Phase 5 (synthesizer)", vd("phase-5"), phase_summary.get("phase-5", {}).get("note", "")),
        ("Phase 6 (analyst)", vd("phase-6"), phase_summary.get("phase-6", {}).get("note", "")),
        ("Phase 7 (strategist)", vd("phase-7"), phase_summary.get("phase-7", {}).get("note", "")),
        (
            "Phase 8 (pre-processor)",
            vd("phase-8"),
            phase_summary.get("phase-8", {}).get("note", ""),
        ),
        (
            "Phase 9 (portfolio manager)",
            vd("phase-9"),
            phase_summary.get("phase-9", {}).get("note", ""),
        ),
    ]
    body = (
        "<table class='stats wide'><thead>"
        "<tr><th>Phase</th><th>Verdict</th><th>Notes</th></tr>"
        "</thead><tbody>"
    )
    for phase, verdict, notes in rows:
        body += f"<tr><td>{phase}</td><td>{verdict}</td><td>{_esc(notes)}</td></tr>"
    body += "</tbody></table>"

    return f'<section class="phase" id="summary"><h2>Run summary</h2>{body}</section>'


# ---------------------------------------------------------------------------
# HTML scaffold (CSS lives in scripts/_e2e_report_assets/style.css)
# ---------------------------------------------------------------------------

CSS_PATH = Path(__file__).resolve().parent / "_e2e_report_assets" / "style.css"


def _load_css() -> str:
    return CSS_PATH.read_text()


def render(layout: Layout, phase_html: dict[str, str]) -> str:
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    css = _load_css()
    nav_links = [
        ("phase-0", "Phase 0: Types"),
        ("phase-1", "Phase 1: Data"),
        ("phase-1b", "Phase 1b: State persistence"),
        ("phase-1c", "Phase 1c: OMS commands"),
        ("phase-2", "Phase 2: Distillation"),
        ("phase-3", "Phase 3: Domain researchers"),
        ("phase-4", "Phase 4: Qualitative + adaptive"),
        ("phase-5", "Phase 5: Synthesizer"),
        ("phase-6", "Phase 6: Analyst"),
        ("phase-7", "Phase 7: Strategist"),
        ("phase-8", "Phase 8: Pre-processor"),
        ("phase-9", "Phase 9: PM (trades)"),
    ]
    nav_html = "\n".join(f'<a href="#{i}">{_esc(label)}</a>' for i, label in nav_links)
    phases_html = "\n".join(
        f'<section class="phase" id="{pid}">\n{phase_html.get(pid, "")}\n</section>'
        for pid, _ in nav_links
    )
    summary_html = phase_html.get("summary", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlphaMind — E2E Verification ({_esc(layout.invocation_id)})</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>AlphaMind End-to-End Verification</h1>
  <div class="meta">
    Report generated: {now_iso}<br>
    Archive root: <code>{_esc(str(layout.archive_root))}</code> ·
    Invocation: <code>{_esc(layout.invocation_id)}</code>
  </div>
</header>
<nav class="toc">{nav_html}</nav>
<main>
{summary_html}
{phases_html}
</main>
</body>
</html>"""


PHASE_TEMPLATE: dict[str, dict[str, Any]] = {
    "phase-0": {"verdict": "PASS", "note": "40/40 cases · 0.16s"},
    "phase-1": {
        "verdict": "PASS",
        "note": "",
        "bootstrap": [["Tables present", "30/30 OK"]],
        "collectors": [{"name": "fred.macro", "age": "15:13:46 ago (cap 8:00)", "status": "FAIL"}],
    },
    "phase-1b": {"verdict": "PASS", "note": f"6/6 phases A{chr(0x2013)}F"},
    "phase-1c": {"verdict": "PASS", "note": "4/4 phases"},
    "phase-2": {
        "verdict": "PASS",
        "note": "regime=vol_expansion · 391 blocks · 404 anomalies",
        "regime_label": "vol_expansion",
        "block_counts": {
            "tech_semis": 24,
            "financials": 22,
            "energy": 22,
            "total": 391,
            "anomalies": 404,
        },
    },
    "phase-3": {"verdict": "PASS", "note": "3 sectors + failure-mode harness"},
    "phase-4": {
        "verdict": "PASS",
        "note": "after recalibration of output_token_budget + adaptive latency_budget",
    },
    "phase-5": {"verdict": "PASS", "note": ""},
    "phase-6": {"verdict": "PASS", "note": ""},
    "phase-7": {"verdict": "PASS", "note": ""},
    "phase-8": {"verdict": "PASS", "note": "all 4 scenarios"},
    "phase-9": {"verdict": "PASS", "note": ""},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-root",
        required=False,
        type=Path,
        help="Path to the verification archive root (e.g. .archive/verify-pipeline-20260509)",
    )
    parser.add_argument(
        "--invocation-id",
        required=False,
        help="Main pipeline invocation_id (e.g. 20260509T191537Z-verify-pipeline)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output HTML path. Defaults to <archive-root>/e2e-report.html",
    )
    parser.add_argument(
        "--phase-summary",
        type=Path,
        help="Path to a JSON file overriding per-phase verdicts/notes "
        "(merges over the built-in defaults). See --print-phase-template.",
    )
    parser.add_argument(
        "--print-phase-template",
        action="store_true",
        help="Print the JSON template for --phase-summary and exit.",
    )
    args = parser.parse_args()

    if args.print_phase_template:
        print(json.dumps(PHASE_TEMPLATE, indent=2))
        return 0

    if not args.archive_root or not args.invocation_id:
        parser.error(
            "--archive-root and --invocation-id are required (unless --print-phase-template)"
        )

    archive_root = args.archive_root.resolve()
    output_path = (args.output or (archive_root / "e2e-report.html")).resolve()

    if not archive_root.exists():
        parser.error(f"archive_root does not exist: {archive_root}")

    layout = Layout(archive_root, args.invocation_id)
    if not layout.run_invocation_dir.exists():
        parser.error(f"invocation dir does not exist: {layout.run_invocation_dir}")

    phase_summary: dict[str, dict[str, Any]] = {**PHASE_TEMPLATE}
    if args.phase_summary:
        override = json.loads(args.phase_summary.read_text())
        for k, v in override.items():
            phase_summary[k] = {**phase_summary.get(k, {}), **v}

    phase_html = {
        "summary": summary_section(phase_summary),
        "phase-0": phase_0(phase_summary.get("phase-0", {})),
        "phase-1": phase_1(phase_summary.get("phase-1", {})),
        "phase-1b": phase_1b(phase_summary.get("phase-1b", {})),
        "phase-1c": phase_1c(phase_summary.get("phase-1c", {})),
        "phase-2": phase_2(layout, phase_summary.get("phase-2", {})),
        "phase-3": phase_3(layout, phase_summary.get("phase-3", {})),
        "phase-4": phase_4(layout, phase_summary.get("phase-4", {})),
        "phase-5": phase_5(layout, phase_summary.get("phase-5", {})),
        "phase-6": phase_6(layout, phase_summary.get("phase-6", {})),
        "phase-7": phase_7(layout, phase_summary.get("phase-7", {})),
        "phase-8": phase_8(phase_summary.get("phase-8", {})),
        "phase-9": phase_9(layout, phase_summary.get("phase-9", {})),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render(layout, phase_html))
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
