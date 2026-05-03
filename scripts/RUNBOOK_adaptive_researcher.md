# Adaptive-Researcher End-to-End Verification Runbook

Operator workflow for the ALP-265 verification artifacts that prove the
adaptive-researcher work tree (ALP-112) works end-to-end. Run after a
`git pull` that touches `src/alphamind/analysis/adaptive_research/` or
its dependencies (the seven on-demand tools, `agents.yaml`,
`prompts/analysis/adaptive_researcher.md`, the input-bundle assembler,
or the harness).

Per parent issue § Pre-resolved configuration decisions (H), this
verification fakes the upstream pipeline composition by loading
recorded fixtures of `(tuple[SectorBrief, ...], QualitativeBrief,
CorrelationRegimeBrief, DistillationOutputs, universal_regime_label)`
rather than running domain researchers + qualitative + distillation
live. Live composition lands in the separate execution-layer wiring
story tracked in `docs/project-tracker.md` § Substantial.

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script cannot run without it.
2. **Populated database accessible.** On macOS the production database
   is at `/Volumes/Users/jacks/AlphaMind/data/alphamind.db`; on Windows
   the production server resolves it via `DATABASE_PATH` or `main.yaml`.
   The harness's in-process tools read news, prediction-market, and
   macro tables via the SQLAlchemy session — an empty DB still lets the
   runner complete but the agent's tool calls return empty payloads.
3. **Recorded upstream-brief fixtures present.** The fixtures live at
   `tests/analysis/adaptive_research/fixtures/` and ship in the
   repository. They are hand-constructed typed-object payloads (three
   `SectorBrief` instances, one `QualitativeBrief`, one
   `CorrelationRegimeBrief`, one multi-block `DistillationOutputs`, and
   the `universal_regime_label` `dict[str, Any]`) sufficient to
   exercise the all-four-sections code path through the input-bundle
   assembler and the validator's six-prefix Layer-3 reference universe.
4. **`uv sync` completed** — the script runs under `uv run` so the
   package and its dev dependencies must be installed.

## Run the verification

### Manual one-shot script

```bash
# Real-SDK end-to-end verification (consumes Sonnet cap; ~30-60s).
uv run python scripts/verify_adaptive_researcher.py \
    --as-of 2026-05-02T14:30:00Z \
    --archive-root .archive/verify-adaptive
```

CLI flags:

- `--invocation-id INV` — override the invocation_id used for the
  diagnostic-archive layout. Defaults to
  `YYYYMMDDTHHMMSSZ-verify-adaptive-researcher`.
- `--as-of TS` — ISO-8601 timestamp for the current market snapshot;
  defaults to now (UTC). Drives the input bundle's `as_of` header and
  the freshness floor of the assembled anomaly inputs.
- `--db-path PATH` — override the SQLite database location.
- `--archive-root DIR` — override the invocation-archive root. Defaults
  to `%USERPROFILE%/AlphaMind/archive` on Windows,
  `~/AlphaMind/archive` elsewhere.

### CI-friendly automated test

```bash
# Skipped by default; runs only when RUN_LIVE_LLM_TESTS=1 is set
# (and CLAUDE_CODE_OAUTH_TOKEN is in the environment).
RUN_LIVE_LLM_TESTS=1 \
    uv run pytest -m e2e tests/analysis/adaptive_research/test_e2e.py -n auto
```

Without the env-var, the test is skipped with a clear reason and the
file's non-e2e structural fixture-shape assertions still run.

## Expected output

```
======================================================================
AlphaMind Adaptive-Researcher End-to-End Verification
======================================================================

[ Invocation summary ]
  invocation_id              = 20260502T143000Z-verify-adaptive-researcher
  wall_clock                 = 42.18s
  input_tokens               = 8400
  output_tokens              = 720
  cache_read_tokens          = 0
  cache_write_tokens         = 0
  tool_calls_used            = 11
  retry_count                = 0
  thread_count               = 3
  anomalies_triaged_count    = 5
  anomalies_deferred_count   = 1

[ Diagnostic archive ]
  adaptive_researcher    files=5/5 OK

[ Input bundle sections ]
  adaptive_researcher    sections=4/4 OK

[ Budget envelope ]
  wall_clock = 42.18s (budget 300.00s, loaded from agents.yaml)
  output_tokens = 720 (budget 1500, loaded from agents.yaml)
  tool_calls_used = 11 (limit 25, loaded from agents.yaml)

[ Brief preview ]
invocation_id: 20260502T143000Z-verify-adaptive-researcher
threads_investigated_count: 3
anomalies_triaged_count: 5
anomalies_deferred: ['SA-FIN-ANOM-1']

=== INVESTIGATION THREADS ===
  [AR-1] signal (moderate) — tech_semis — tickers=['NVDA']
    trigger:  SA-TECH-ANOM-1: NVDA volume 2.4x ADV with no price drift
    question: Is institutional accumulation building behind NVDA's stealth bid?
    tools_used: ['news_search', 'short_interest', 'prediction_markets']
    - finding: short_interest declined 8% week-over-week, supports accumulation thesis
    - finding: PM Q1-beat odds rose to 0.62, consistent with positive flow
    implication: Sustains the buy-the-dip thesis on NVDA selloffs.
    strengthens: ['SA-TECH-1', 'SA-TECH-TC-1', 'QR-1']
    weakens: []
...

======================================================================
RESULT: PASS — every structural assertion holds.
======================================================================
```

Exit code: `0` on PASS, `1` on FAIL.

## Refresh fixtures when an upstream contract changes

The fixtures are checked into the repository under
`tests/analysis/adaptive_research/fixtures/`. Refresh when one of the
following lands:

- A new field on `SectorBrief`, `QualitativeBrief`,
  `CorrelationRegimeBrief`, or any nested record type.
- A new prefix family the validator's
  `_build_reference_universe` understands.
- A change to `OutputBlock`, `AnomalyFlag`, or `DistillationOutputs`
  shape.
- A change to the canonical regime payload keys
  (`regime`, `transition_flag`, `confidence`, `freshness_ts`) the
  input-bundle renderer reads.

To refresh:

1. Edit the relevant `.json` file under
   `tests/analysis/adaptive_research/fixtures/` to match the new
   contract. The loader in
   `tests/analysis/adaptive_research/fixtures/__init__.py` is
   per-field-typed, so a missing or renamed field surfaces as a
   construction error at fixture load time.
2. Re-run the non-e2e portion of the suite:
   `uv run pytest tests/analysis/adaptive_research/test_e2e.py -n auto`.
3. Commit the refreshed fixtures alongside the upstream-contract change
   in the same PR — the fixtures encode the contract.

The dev-machine production database at
`/Volumes/Users/jacks/AlphaMind/data/alphamind.db` is NOT a source for
these fixtures; they are deterministic hand-constructed payloads that
exercise the renderer's full code path without DB dependence.

## Failure-mode triage

If the script exits non-zero, follow the failure-code triage table:

| Failure code | Root cause likely lives in | First place to look |
|---|---|---|
| `brief-re-validation-failed` | The adaptive-researcher prompt or the parser in `src/alphamind/analysis/adaptive_research/parser.py`. The brief reached a structural shape the validator rejects, OR a Strengthens/Weakens reference doesn't resolve to any fixture upstream ID. | The error message names the field path and validator rule; cross-check against `docs/design/03-analysis-layer/adaptive-research.md` § Output § Output schema and `tests/analysis/adaptive_research/fixtures/` for the expected referential universe. |
| `diagnostic-archive-missing-file` | The harness's archive write in `src/alphamind/analysis/adaptive_research/harness.py` (`_DiagState.write`). Expected files: `prompt.md`, `user_message.md`, `response_initial.md`, `errors.json`, `metadata.json`. | Check `<archive_root>/invocations/<invocation_id>/analysis/adaptive_researcher/` — list its contents and compare against the message. |
| `input-bundle-section-missing` | The input-bundle assembler in `src/alphamind/analysis/adaptive_research/input_bundle.py`. One of the four section markers is missing from the rendered text. | The renderer always emits all four section headers (header, regime, distillation, sector); a missing marker signals a regression in the assembler, not in the agent. |
| `tool-name-outside-allowlist` | Either the prompt invited the agent to cite a tool that isn't registered, or the agent hallucinated a tool name. | Cross-check `prompts/analysis/adaptive_researcher.md` against `config/agents.yaml` § `adaptive_researcher.tools`; the seven registered tools are `news_search`, `prediction_markets`, `sec_lending`, `short_interest`, `earnings_calendar`, `macro_data`, `ticker_deep_pull`. |
| `wall-clock-budget-exceeded` | Either the SDK is actually slow (Anthropic incident, regional latency) or `latency_budget_seconds` in `config/agents.yaml` was tightened below realistic. | The 300s default is generous; recurrent breaches signal an upstream regression in the SDK or the prompt. |
| `output-token-budget-exceeded` | The brief ballooned past the 1500 nominal target. Could be a prompt regression that loosened the budget directive, or the LLM ignored it. | `docs/design/03-analysis-layer/adaptive-research.md` § Output § Token budget; cross-check `prompts/analysis/adaptive_researcher.md` for the budget directive. |
| `tool-call-limit-exceeded` | The agent looped past `cumulative_tool_call_limit`. Either the prompt invited too many tool calls, or the harness lost track of the cap. | `docs/design/03-analysis-layer/adaptive-research.md` § Tool budgets; check the harness's `tool_calls_used` accounting against the live SDK transcript. |
| `RuntimeError: CLAUDE_CODE_OAUTH_TOKEN is not set` | Missing environment variable. | `claude setup-token`; export the token in the shell session before re-running. |
| SDK auth error (`SDKFailure` raised inside the verification) | Token expired, was revoked, or the cap-enforcement classifier rejected it. | `docs/design/cost-and-rate-limit-modeling.md` § Authentication / Residual technical-enforcement risk. Re-issue the token; if rejection persists, switch to API-key auth per § API-key escape hatch. |

For any failure not on the table: rerun with `--archive-root` pointing
at a writable scratch dir, then inspect
`<archive_root>/invocations/<invocation_id>/analysis/adaptive_researcher/`
for the raw prompt + response + error trail the harness wrote.

## Known limitation: faked upstream pipeline

This verification fakes the upstream pipeline composition. The recorded
fixture tuple is fed directly into `run_adaptive_researcher` instead of
being produced by the upstream `run_external_distillation`,
`run_qualitative_researcher`, and three sector researchers. The
separate execution-layer wiring story (tracked in
`docs/project-tracker.md` § Substantial) is responsible for live
composition and the orchestrator's fail-closed propagation across the
combined pipeline.
