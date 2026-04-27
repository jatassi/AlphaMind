---
status: not_started
completed_date:
commit_id:
---

# 11 — End-to-end live-SDK verification

## Goal

Verify the full synthesizer pipeline against the real Claude Agent SDK using a canonical fixture set spanning all six upstream brief sources. Not a unit test — an executable verification script the operator runs when the work tree completes (and ad-hoc thereafter to confirm the integration still holds across SDK / Anthropic-API changes). Mirrors the domain-researcher tree's [story 12](../domain-researchers/12-end-to-end-verification.md) end-to-end verification posture.

## Reading

- `docs/architecture/llm-integration.md` § Authentication — `CLAUDE_CODE_OAUTH_TOKEN` setup; the script needs a valid token
- `docs/design/cost-and-rate-limit-modeling.md` — the script consumes a measurable slice of the weekly Sonnet cap; the operator should know this before running it casually
- `docs/implementation/03-analysis-layer/synthesizer/10-synthesizer-runner.md` — the runner's API
- `docs/implementation/03-analysis-layer/synthesizer/09-system-prompt.md` — the prompt being verified
- `docs/implementation/03-analysis-layer/domain-researchers/12-end-to-end-verification.md` — sibling verification's posture, fixture style, and operator-facing reporting format

## Depends on

- 09 (system prompt — must exist on disk)
- 10 (runner — the script's entry point)
- All preceding stories (the integration is meaningful only when every component is on `main`)

## Scope

In scope: under `scripts/` (or wherever the sibling verification scripts live — match that location) —

- A standalone verification script `verify_synthesizer.py` that:

  1. Builds a canonical fixture `UpstreamBriefs`:
     - Three sector briefs constructed via the domain-researcher tree's models (`SectorBrief`) — small, hand-written briefs with 3 findings + 1 anomaly + 1 thesis-candidate per sector, each with realistic-but-synthetic ticker / signal / detail content. The corresponding raw text is rendered to match the format the parser expects.
     - One `CorrelationRegimeBrief` constructed via the distillation tree's value type — a regime-stable brief with 4–6 `[CR-N]` findings spanning regime, intra-sector, lead-lag.
     - One qualitative-brief raw text with 2 `[QR-N]` narrative threads and 1 `[QR-CW-N]` catalyst entry — hand-written to match the qualitative-research output schema.
     - One adaptive-research raw text with 2 `[AR-N]` investigation threads — hand-written to match the adaptive-research output schema.
     - A `RegimeLabel` matching the CR brief's embedded regime.
     The fixture lives in the script (or a sibling `fixtures/` module) so the script is self-contained and operators can read what synthesis is being requested.

  2. Builds a `StubPortfolioStateReader` (story 05b) representing a small held book — 3 positions across the three sectors, 3 active theses tied to those positions, and a corresponding exposure snapshot. Realistic-enough that the synthesizer has portfolio-relevant context to potentially cross-reference against.

  3. Loads the synthesizer's `AgentConfig` from `config/agents.yaml` via the existing config loader.

  4. Calls `await run_synthesizer(invocation_id="VERIFY-{utcnow}", as_of=utcnow, agent_config=cfg, upstream=fixture, portfolio_reader=stub_reader)`.

  5. Reports per the operator-facing format below; on any harness failure, reports the failure type, message, and the diagnostic-archive path.

  6. Exits with code 0 on success, non-zero on failure (so CI / cron / manual operator usage can act on the exit).

- Operator report format (printed to stdout):

  ```
  === SYNTHESIZER VERIFICATION ===
  Invocation: VERIFY-{utcnow}
  Model: {agent_config.model}
  Wall clock: {wall_clock_seconds:.2f}s
  Tokens: input {tokens_used.input} / output {tokens_used.output}
  Tool calls: {tool_call_count}
  Stop reason: {stop_reason}

  === SYNTHESIS TEXT ===
  {synthesis_text}

  === REFERENCE COVERAGE ===
  References cited in synthesis: {N total}
  Unique reference IDs: {M}
  By prefix:
    SA-TECH: {n cited} / {n in retrieval store}
    SA-FIN:  ...
    SA-ENERGY: ...
    QR:      ...
    QR-CW:   ...
    AR:      ...
    CR:      ...
  Invented references (cited but not in store): {list, empty on healthy run}
  Unknown markers from upstream: {list per source, empty on healthy run}

  === DIAGNOSTIC RECORD ===
  Path: {archive path}
  Files: prompt.md, user_message.md, response.md, metadata.json (sizes)

  === VERDICT ===
  {one of: PASS / WARN / FAIL with reason}
  ```

  Verdict logic:
  - **FAIL** — runner raised any failure, OR the synthesis text is empty, OR the synthesis cited any invented reference (well-formed reference ID that does not resolve in the retrieval store — the same Layer 3 check the consumer-side validator does).
  - **WARN** — synthesis cited fewer than two distinct prefix families (low cross-source connectivity is suspect on a six-source fixture) OR `unknown_markers_by_source` is non-empty for any source (an upstream produced malformed text — a fixture problem worth flagging, not a synthesizer problem).
  - **PASS** — non-empty synthesis, every cited reference resolves, at least two distinct prefix families cited, no upstream unknown markers.

- Reference-coverage post-analysis (script-internal, not part of the runner): scan `synthesis_text` for `[<TOKEN>]` patterns and resolve each via `parse_reference_id` + `RetrievalStore.lookup`. The result feeds the report's "References cited" / "Invented references" sections. This logic is the script's own — it is the consumer-side Layer 3 check, applied here so the verification surfaces what the future analyst / strategist / PM Layer 3 validators would catch.

- Documentation: a `README.md` in the same directory (or appended to the work-tree's existing README) explaining:
  - When to run the script (after the work tree completes; periodically as a smoke test; before merging changes that affect any of the synthesizer's components).
  - The cost — one Sonnet call per run (~10K input + ~1.5K output).
  - The expected exit code and what FAIL / WARN signal.
  - The auth prerequisite — `CLAUDE_CODE_OAUTH_TOKEN` must be set; missing token surfaces as a clean `SDKFailure` from the harness, which the script reports.

- Unit tests:
  - The script imports cleanly (`python -c "import scripts.verify_synthesizer"` from the repo root).
  - The reference-coverage post-analysis correctly classifies a synthetic synthesis text containing one valid and one invented reference.
  - The verdict logic returns FAIL / WARN / PASS on the documented inputs.
  - The script's fixture-construction code produces an `UpstreamBriefs` value that passes the runner's input validation (no need to actually invoke the SDK in this test).

Out of scope:
- Running the script itself in CI — this script consumes real Anthropic API quota; CI runs are operator-triggered.
- Comparing the synthesis text against a "golden" expected output — LLM responses are non-deterministic; the verification's signal is structural (the synthesis happened, it cited references that resolve, it covered enough sources), not content-equivalent.
- A budget-tracking dashboard for the script — the operator runs it manually; per-run cost is small.
- Multi-model verification (running against Haiku as well as Sonnet) — out of scope for v1; can be added later as a flag if the operator wants it.

## Notes

The script is the only place in this work tree that calls the real Anthropic API. Every other test stubs the SDK (per the unit-test discipline in stories 06a / 06b / 08 / 10). Treating the live verification as a separate operator-triggered script keeps the regular test suite fast, deterministic, and free of API-quota dependencies — and gives the operator an explicit "I want to spend the API quota now" surface.

The reference-coverage post-analysis is the script's principal value-add over a pass/fail-only verification. It surfaces:
- How densely the synthesizer cited (low citation density indicates the synthesizer is producing summary-style prose without grounding — the `narrative_padding` anti-pattern from story 09).
- Whether all six upstream sources were touched (low cross-source coverage indicates the synthesizer is over-focused on one source — failing the "intersections across vantage points" mandate).
- Whether any cited reference is invented (forward-detection of the same Layer 3 failure the consumer-side validators catch — surfacing it here lets the operator catch system-prompt drift before it ships to the decision-layer agents).

The WARN verdict for "fewer than two distinct prefix families" is calibrated against the six-source fixture. If the operator runs the script against a fixture with fewer sources, this threshold is misleading — but the script's fixture is fixed, so the calibration holds. Document this clearly so future modifications to the fixture also update the threshold.

The script's fixture construction is the slowest part of authoring this story (each sector brief, qualitative thread, and AR thread needs to be hand-written to look realistic). Borrow the fixture style from the domain-researcher tree's story 12 fixtures where possible; the synthesizer's job is the same regardless of upstream content, so synthetic-but-plausible fixture content is fine. Per [`feedback_avoid_numeric_anchors.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_avoid_numeric_anchors.md), do NOT lift the fixture content to live data — the script is a smoke test, not a backtest, and live data drifts.

Per CLAUDE.md, the script is operator-facing and prints to stdout — keep the report under one screen of terminal output where possible. The synthesis text dominates the height; the rest of the sections are headers and short lists.

The script does NOT amend the diagnostic record's `metadata.json` beyond what the runner writes — its reference-coverage analysis is reported to stdout but not persisted. If a future story wants to persist verification runs for trend-tracking, that's a separate piece of work.

## Acceptance criteria

- [ ] `scripts/verify_synthesizer.py` exists and is importable.
- [ ] Running the script with a valid `CLAUDE_CODE_OAUTH_TOKEN` produces the documented operator report.
- [ ] The reference-coverage analysis correctly classifies valid vs. invented references in the synthesis text.
- [ ] The verdict logic returns PASS on a healthy run, FAIL on harness failure / empty synthesis / invented references, WARN on low cross-source citation density / upstream unknown markers.
- [ ] The script exits 0 on PASS / WARN, non-zero on FAIL.
- [ ] A README near the script documents when to run, cost, expected outputs, and the auth prerequisite.
- [ ] Unit tests cover the reference-coverage classifier and the verdict logic against synthetic inputs (no real SDK call required for the unit tests).
- [ ] Missing `CLAUDE_CODE_OAUTH_TOKEN` surfaces as a clean `SDKFailure` reported by the script, not an unhandled exception.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
