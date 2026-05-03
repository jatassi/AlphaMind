# Qualitative-Researcher End-to-End Verification Runbook

Operator workflow for the ALP-253 verification artifacts that prove the
qualitative-researcher work tree (ALP-111) works end-to-end. Run after a
`git pull` that touches `src/alphamind/analysis/qualitative_research/`
or its dependencies (news-clustering pipeline, `agents.yaml`,
`prompts/analysis/qualitative_researcher.md`).

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script cannot run without it.
2. **Populated database with at least one recent distillation invocation.**
   On macOS the production database is at
   `/Volumes/Users/jacks/AlphaMind/data/alphamind.db`; on Windows
   the production server resolves it via `DATABASE_PATH` or `main.yaml`.
   When the database has no `distillation_regime_state` rows the script
   falls back to a synthetic regime-label stub and surfaces the choice
   in stdout.
3. **`uv sync` completed** — the script runs under `uv run` so the
   package and its dev dependencies must be installed.

## Run the verification

### Manual one-shot script

```bash
# Real-SDK end-to-end verification (consumes Sonnet cap; ~15-30s).
uv run python scripts/verify_qualitative_researcher.py \
    --as-of 2026-05-02T14:30:00Z \
    --last-invocation 2026-05-02T13:30:00Z \
    --archive-root .archive/verify-qual
```

CLI flags:

- `--as-of TS` — ISO-8601 timestamp for the current market snapshot;
  defaults to now (UTC).
- `--last-invocation TS` — ISO-8601 timestamp for the prior invocation;
  bounds the news-digest window; defaults to `as_of - 1h`.
- `--db-path PATH` — override the SQLite database location.
- `--archive-root DIR` — override the invocation-archive root. Defaults
  to `%USERPROFILE%/AlphaMind/archive` on Windows,
  `~/AlphaMind/archive` elsewhere.

### CI-friendly automated test

```bash
# Skipped by default; runs only when RUN_LIVE_LLM_TESTS=1 is set.
RUN_LIVE_LLM_TESTS=1 \
    uv run pytest tests/analysis/qualitative_research/test_e2e.py -n auto
```

## Expected output

```
======================================================================
AlphaMind Qualitative-Researcher End-to-End Verification
======================================================================

[ Invocation summary ]
  invocation_id     = 20260502T143000Z-verify-qualitative-researcher
  wall_clock        = 15.42s
  input_tokens      = 6500
  output_tokens     = 480
  cache_read_tokens = 0
  cache_write_tokens= 0
  tool_calls_used   = 4
  retry_count       = 0
  thread_count      = 3
  catalyst_count    = 2
  signal_quality    = high
  digest_collected  = 28
  digest_shown      = 12

[ Diagnostic archive ]
  qualitative_researcher    files=5/5 OK

[ Budget envelope ]
  wall_clock = 15.42s (budget 180.00s, loaded from agents.yaml)
  output_tokens = 480 (budget 1000, loaded from agents.yaml)
  tool_calls_used = 4 (limit 15, loaded from agents.yaml)

[ Brief preview ]
signal_quality: high

=== NARRATIVE THREADS ===
  [QR-1] NVDA — bullish (near_term)
    summary: ...
    - news_digest: ... (ND-T1)
    - prediction_market: ... (PM-001)
    implication: ...
...

=== CATALYST WATCH ===
  [QR-CW-1] NVDA — earnings (in 24h)
    thesis_impact: ...

=== SENTIMENT SNAPSHOT ===
  extremes: ...
  divergences: ...
  regime: risk-on

======================================================================
RESULT: PASS — every structural assertion holds.
======================================================================
```

Exit code: `0` on PASS, `1` on FAIL.

## Failure-mode triage

If the script exits non-zero, follow the failure-code triage table:

| Failure code | Root cause likely lives in | First place to look |
|---|---|---|
| `brief-re-validation-failed` | The qualitative-researcher prompt or the parser in `src/alphamind/analysis/qualitative_research/parser.py`. The brief reached a structural shape the validator rejects. | The error message names the field path and validator rule; cross-check against `docs/design/03-analysis-layer/qualitative-research.md` § Output § Output schema. |
| `diagnostic-archive-missing-file` | The harness's archive write in `src/alphamind/analysis/qualitative_research/harness.py` (`_DiagState.write`). The expected files are `prompt.md`, `user_message.md`, `response_initial.md`, `errors.json`, `metadata.json`. | Check `<archive_root>/invocations/<invocation_id>/analysis/qualitative_researcher/` — list its contents and compare against the message. |
| `wall-clock-budget-exceeded` | Either the SDK is actually slow (Anthropic incident, regional latency) or `latency_budget_seconds` in `config/agents.yaml` was tightened below realistic. | `docs/design/cost-and-rate-limit-modeling.md` § Latency budgets — qualitative_researcher's 180s ceiling is generous; recurrent breaches signal an upstream regression. |
| `output-token-budget-exceeded` | The brief ballooned past the 300–600 nominal target. Could be a prompt regression that loosened the budget directive, or the LLM ignored it. | `docs/design/03-analysis-layer/qualitative-research.md` § Output § Token budget; cross-check `prompts/analysis/qualitative_researcher.md` for the budget directive. |
| `tool-call-limit-exceeded` | The agent looped past `cumulative_tool_call_limit`. Either the prompt invited too many tool calls, or the harness lost track of the cap. | `docs/design/03-analysis-layer/qualitative-research.md` § Tool budgets; check the harness's `tool_calls_used` accounting against the live SDK transcript. |
| `RuntimeError: CLAUDE_CODE_OAUTH_TOKEN is not set` | Missing environment variable. | `claude setup-token`; export the token in the shell session before re-running. |
| SDK auth error (`SDKFailure` raised inside the verification) | Token expired, was revoked, or the cap-enforcement classifier rejected it. | `docs/design/cost-and-rate-limit-modeling.md` § Authentication / Residual technical-enforcement risk. Re-issue the token; if rejection persists, switch to API-key auth per § API-key escape hatch. |
| News digest empty / `news_digest.entries` empty | The clustering pipeline hasn't populated `news_article_clusters` for the `[last_invocation, as_of)` window. | Check `news_articles` for rows in the window, and `news_article_clusters` for cluster rows referencing them. |
| `universal_regime_label source: synthetic-stub` warning | The DB has no `distillation_regime_state` rows. Verification still runs but the regime block is the synthetic stub from the script. | Run a distillation pass first, or treat the verification as a smoke test only. |

For any failure not on the table: rerun with `--archive-root` pointing
at a writable scratch dir, then inspect
`<archive_root>/invocations/<invocation_id>/analysis/qualitative_researcher/`
for the raw prompt + response + error trail the harness wrote.
