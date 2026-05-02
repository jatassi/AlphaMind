# Domain-Researcher End-to-End Verification Runbook

Operator workflow for the two ALP-186 verification scripts that prove the
domain-researcher layer (ALP-113) works end-to-end. Run after a
`git pull` that touches `src/alphamind/analysis/domain_researchers/` or
its dependencies (distillation orchestrator, agents.yaml,
`prompts/analysis/*_researcher.md`).

## Prerequisites

1. **`CLAUDE_CODE_OAUTH_TOKEN` set** — generate via `claude setup-token`
   per `docs/architecture/llm-integration.md` § Authentication. The
   real-SDK script (`verify_domain_researchers.py`) cannot run without it.
   The failure-mode script (`verify_domain_researcher_failure_modes.py`)
   does not require it.
2. **Populated database with at least one recent distillation invocation.**
   The verification calls `run_external_distillation` against the universe
   scope from `config/assets.yaml` at "now"; an empty database produces
   empty sector outputs and the researchers will reject them.
   On macOS the production database is at
   `/Volumes/Users/jacks/AlphaMind/data/alphamind.db`; on Windows
   the production server resolves it via `DATABASE_PATH` or `main.yaml`.
3. **`uv sync` completed** — both scripts run under `uv run` so the
   package and its dev dependencies must be installed.

## Run the verification

```bash
# 1. Real-SDK end-to-end verification (consumes Sonnet cap; ~30-60s).
uv run python scripts/verify_domain_researchers.py

# 2. Deterministic failure-mode harness (no SDK calls; <2s).
uv run python scripts/verify_domain_researcher_failure_modes.py
```

Optional CLI flags:

- `--db-path PATH` (real-SDK only) — override the SQLite database location.
- `--archive-root DIR` — override the invocation-archive root. Defaults to
  `%USERPROFILE%/AlphaMind/archive` on Windows, `~/AlphaMind/archive`
  elsewhere (real-SDK), or a fresh tempdir (failure-mode).

## Expected output

### Real-SDK script

A summary table covering all three sectors:

```
======================================================================
AlphaMind Domain-Researcher End-to-End Verification
======================================================================

[ Per-sector results ]
  sector           wall_s   in_tok  out_tok  retry  find  anom  thes quality
  tech_semis        12.34     6500     1200      0     5     2     3 high
  financials        10.21     6100     1100      0     4     1     2 high
  energy             9.87     6000     1050      0     3     0     2 moderate

[ Diagnostic archive ]
  tech_semis_researcher     files=4/4 OK
  financials_researcher     files=4/4 OK
  energy_researcher         files=4/4 OK

[ Budget envelope ]
  wall_clock total = 12.34s (budget 120.00s, loaded from agents.yaml)
  tokens total = 21950 (budget 30000, loaded from agents.yaml)
  total retries = 0

======================================================================
RESULT: PASS — every structural assertion holds.
======================================================================
```

Exit code: `0` on PASS, `1` on FAIL.

### Failure-mode script

A per-scenario summary listing all three scenarios:

```
======================================================================
AlphaMind Domain-Researcher Failure-Mode Verification
======================================================================

[ Scenarios ]
  parse-failure-then-success               PASS
    expected: harness retries once on parse failure and accepts the second response
    observed: retry_count=1, brief sector=tech_semis
  two-parse-failures                       PASS
    expected: harness raises MalformedOutputFailure with the sector's agent_name
    observed: raised MalformedOutputFailure(agent_name='tech_semis_researcher')
  validation-failure-then-success          PASS
    expected: harness retries once on validation failure and accepts the second response
    observed: retry_count=1, brief sector=tech_semis

======================================================================
RESULT: PASS — all injection scenarios behaved as expected.
======================================================================
```

Exit code: `0` on PASS, `1` on FAIL.

## Failure-mode triage

If either script exits non-zero, follow the failure-code triage table:

| Failure code / symptom | Root cause likely lives in | First place to look |
|---|---|---|
| `brief-re-validation-failed` | The researcher prompt or the parser in `src/alphamind/analysis/domain_researchers/parser.py`. The brief reached a structural shape the validator rejects. | The error message names the field path and validator rule; cross-check against `docs/design/03-analysis-layer/domain-researchers/<sector>.md` § Domain researcher output contract. |
| `diagnostic-archive-missing-file` | The harness's archive write in `src/alphamind/analysis/domain_researchers/harness.py` (`_DiagState.write`). The expected files are `prompt.md`, `user_message.md`, `response_initial.md`, `metadata.json`. | Check `<archive_root>/invocations/<invocation_id>/analysis/<agent_name>/` — list its contents and compare against the message. |
| `wall-clock-budget-exceeded` | Either the SDK is actually slow (Anthropic incident, regional latency) or `latency_budget_seconds` in `config/agents.yaml` was tightened below realistic. | `docs/design/cost-and-rate-limit-modeling.md` § Latency model — Sonnet researchers normally finish in 8–15s each (parallel), so a 120s ceiling is generous. Recurrent breaches signal an upstream regression. |
| `token-budget-exceeded` | The input bundle ballooned (qualitative input loader returning too many headlines, distillation text growing) or `context_token_budget` / `output_token_budget` in `config/agents.yaml` was tightened. | `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate. Check the per-sector `in_tok` / `out_tok` columns in the summary; the offender will be obvious. |
| Failure-mode scenario `parse-failure-then-success` FAIL | The corrective-retry path in `harness.py` regressed; retry is not firing. | `tests/analysis/domain_researchers/test_harness.py` exercises this path in isolation — re-run those tests to confirm. |
| Failure-mode scenario `two-parse-failures` FAIL with wrong exception type | The harness's exception hierarchy regressed. | `harness.py` § exception-class definitions and `_parse_and_validate`. |
| Failure-mode scenario `validation-failure-then-success` FAIL | Same as the parse-failure variant — the corrective-retry path regressed. | Same as above. |
| `RuntimeError: CLAUDE_CODE_OAUTH_TOKEN is not set` | Missing environment variable. | `claude setup-token`; export the token in the shell session before re-running. |
| SDK auth error (`SDKFailure` raised inside the verification) | Token expired, was revoked, or the cap-enforcement classifier rejected it. | `docs/design/cost-and-rate-limit-modeling.md` § Authentication / Residual technical-enforcement risk. Re-issue the token; if rejection persists, switch to API-key auth per § API-key escape hatch. |
| `KeyError` on a sector entry in `sectors_config` | `config/assets.yaml` was edited and the `tech` / `semis` / `financials` / `energy` sector keys were renamed or removed. | `load_sectors_config` in `src/alphamind/scripts/verify_domain_researchers.py` reads those four keys. Restore the keys (or update both the loader and the script in tandem). |

For any failure not on the table: rerun with the `--archive-root` flag
pointing at a writable scratch dir, then inspect
`<archive_root>/invocations/<invocation_id>/analysis/<agent_name>/`
for the raw prompt + response + error trail the harness wrote.
