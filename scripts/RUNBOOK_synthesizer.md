# Synthesizer End-to-End Live-SDK Verification Runbook

Operator workflow for the ALP-211 verification artifact that proves the
synthesizer work tree (ALP-114) talks to the real Claude Agent SDK
correctly. Run after a `git pull` that touches
`src/alphamind/analysis/synthesizer/`,
`prompts/analysis/synthesizer.md`, or the synthesizer's
`agents.yaml` slot.

## When to run

After the synthesizer work tree completes (the orchestrator integrates
all sibling stories) and any time the synthesizer's prompt, runner,
harness, or input-bundle assembler changes thereafter. The unit-test
suite covers the reference-coverage classifier and verdict rubric; this
script covers the live-SDK invocation those layers wrap.

## Cost

One invocation consumes a measurable slice of the weekly Sonnet cap:
roughly **10K input tokens plus ~1.5K output tokens** (per
`docs/design/cost-and-rate-limit-modeling.md`). The script issues a
single multi-turn call — three portfolio-state tool calls plus the
final prose generation. Re-running gratuitously eats the cap.

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script cannot run without it; missing the token surfaces as
   a clean `SDKFailure` exception with the documented remediation
   message.
2. **`uv sync` completed** — the script runs under `uv run` so the
   package and its dev dependencies must be installed.

## Run the verification

```bash
# Real-SDK end-to-end verification (consumes Sonnet cap; ~10-30s).
uv run python scripts/verify_synthesizer.py \
    --as-of 2026-05-03T14:30:00Z \
    --archive-root .archive/verify-synthesizer
```

CLI flags:

- `--as-of TS` — ISO-8601 timestamp for the current market snapshot;
  defaults to now (UTC). Bounds the freshness timestamps the fixture
  briefs carry.
- `--invocation-id INV` — override the invocation_id used by the
  diagnostic archive layout. Defaults to
  `YYYYMMDDTHHMMSSZ-verify-synthesizer`.
- `--archive-root DIR` — write the harness's diagnostic archive under
  this directory (`<dir>/invocations/<invocation_id>/analysis/synthesizer/`).
  Omit to skip the diagnostic write.

## Expected output

```
=== Synthesizer live-SDK verification ===
invocation_id: 20260503T143000Z-verify-synthesizer
model: claude-sonnet-4-6
wall_clock: 12.34s
tokens: input=10241 output=1893 cache_read=0 cache_write=0
tool_calls: 3
stop_reason: end_turn

--- Synthesis text ---
...the full prose the synthesizer produced...

--- Reference-coverage analysis ---
Cited references: 7 (AR-1, CR-1, QR-2, QR-CW-1, SA-TECH-1, SA-TECH-3, SA-TECH-TC-1)
Resolved against retrieval store: 7
Invented references: 0 ((none))

--- Verdict: PASS ---
```

Exit code: `0` on PASS or WARN, `1` on FAIL.

## Verdict rubric

| Verdict | Conditions | Exit code |
|---|---|---|
| PASS | Response non-empty, zero invented references, `stop_reason` in `{end_turn, max_tokens}`. | 0 |
| WARN | Response non-empty, ≥1 invented reference, `stop_reason` in `{end_turn, max_tokens}`. The synthesizer produced usable prose but cited a reference not present in the retrieval store — a prompt-tightening signal. | 0 |
| FAIL | Any `HarnessFailure` subclass (`EmptyResponseFailure`, `ContextOverflowFailure`, `SDKFailure`, `TimeoutFailure`). | 1 |

## Failure-mode triage

If the script exits non-zero or reports a failure block:

| Failure type | Root cause likely lives in | First place to look |
|---|---|---|
| `SDKFailure` | Missing or invalid `CLAUDE_CODE_OAUTH_TOKEN`, or an Anthropic-side incident. | The exception message names the remediation; re-run `claude setup-token` and confirm the token is exported into the shell. |
| `TimeoutFailure` | Either the SDK is genuinely slow (Anthropic incident, regional latency) or the synthesizer's `latency_budget_seconds` in `config/agents.yaml` was tightened below realistic. | `docs/design/cost-and-rate-limit-modeling.md` § Latency budgets — the synthesizer's 180s ceiling is generous. |
| `ContextOverflowFailure` | Empty response paired with `stop_reason=max_tokens`. The fixture upstream-brief set is large enough to exhaust the context window — typically a regression in the prompt or input-bundle assembler. | Inspect the harness's diagnostic archive (pass `--archive-root`); compare the rendered `user_message.md` against the prompt's documented input contract. |
| `EmptyResponseFailure` | Non-error SDK response with `stop_reason=end_turn` but zero text content. | Check the diagnostic archive's `response.md` and `metadata.json`; usually a prompt regression that produced only tool calls and never closed with text. |
| `WARN` (non-zero invented references) | Prompt drift: the synthesizer cited a reference ID not present in the retrieval store. The script still passes (exit 0) but flags the citation. | Diff `prompts/analysis/synthesizer.md` against the prior known-good run; if the prompt is unchanged, the LLM is hallucinating reference IDs and the prompt's reference-mechanism section likely needs tightening. |
