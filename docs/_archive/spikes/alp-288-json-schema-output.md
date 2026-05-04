# ALP-288 — JSON-Schema structured output for analysis-layer text-format agents

**Date:** 2026-05-04
**Status:** GO with conditions
**Scope:** Bounded research spike — produces a decision and a per-agent migration cost estimate. The actual migration is a follow-up.

## Question

Replace the strict-text parsers in `adaptive_research`, `qualitative_research`, and `domain_researchers` with the Claude Agent SDK's `output_format = {"type": "json_schema", ...}` mode? Goal: eliminate the brittleness class that produced **10 distinct parser/harness/prompt patches across 7 verifier reruns on 2026-05-04** (preamble tolerance, counts-line phrasing, marker scanning, bracketed-ref extraction, tool-name parens stripping, sector aliases, etc.).

## SDK wiring (confirmed)

- `claude-agent-sdk==0.1.72`, bundled CLI `2.1.126`.
- `ClaudeAgentOptions.output_format: dict[str, Any] | None` (`types.py:1737`) accepts `{"type": "json_schema", "schema": <pydantic schema>}`. The transport layer (`subprocess_cli.py:371-380`) translates this into the `--json-schema <json>` arg passed to the bundled CLI.
- `ResultMessage.structured_output: Any` (`types.py:1091`, populated at `message_parser.py:235`) carries the API-validated payload.
- The harness retry path uses `dataclasses.replace(options, resume=session_id)` (adaptive harness `harness.py:791`); `output_format` set on the original options propagates unchanged through retry. **No retry-path change required beyond setting the field once.**

## Spike methodology

`scripts/spike_alp288_structured_output.py` runs five live SDK calls against `claude-sonnet-4-6` with the `AdaptiveBrief.model_json_schema()` (3.4 KB — no API-side bloat concern):

| # | Scenario | Tools? | Purpose |
|---|----------|--------|---------|
| 1 | Schema-only baseline | no | does the basic mechanism work? |
| 2 | Schema + tools, SIGNAL path | yes | tool-loop coexistence; conditional-field branch A |
| 3 | Schema + tools, NOISE path | yes | conditional-field branch B |
| 4 | Schema + tools + provocations | yes | preamble narration, abbreviated sector, parens on tool names, free-text after bracket refs — all four classes the legacy text parser had to grow tolerances for |
| 5 | Schema + tools, tighter token budget | yes | sanity-check cost regression at production-equivalent budget |

Each scenario reports: `structured_output` populated (Y/N), Pydantic round-trip via `AdaptiveBrief.model_validate` (Y/N), tool calls observed, `stop_reason`, tokens, total cost, wall clock. Spike output: `docs/_archive/spikes/alp-288-spike-output.txt`.

## Spike findings

Run against `claude-sonnet-4-6` (production model):

| label | structured? | pydantic_ok? | tool_calls | stop_reason | wall (s) | cost (USD) |
|-------|-------------|--------------|------------|-------------|----------|------------|
| 1 schema_only_baseline | Y | Y | 1 | end_turn | 27.4 | 0.079 |
| 2 schema_tools_signal | Y | Y | 4 | end_turn | 35.4 | 0.062 |
| 3 schema_tools_noise | Y | Y | 3 | end_turn | 24.8 | 0.048 |
| 4 schema_tools_provocation | Y | Y | 4 | end_turn | 46.1 | 0.075 |
| 5 schema_tools_token_budget | Y | **N** | 4 | end_turn | 17.4 | 0.045 |

Four observations matter for the decision:

1. **Schema enforcement is post-generation, not pre-.** The API rejects payloads that violate the schema's *shape* (enums, types, required fields, regex patterns). It does **not** constrain the *content* of free-form string fields. Scenario 4 confirmed both edges:
   - The model was instructed to use `sector: "tech"`. The structured output came back with `sector: "tech_semis"` — the closed-set enum forced re-derivation. Schema works.
   - The model was instructed to write tool names as `"mock_options_flow (NVDA)"` and to suffix bracket refs with free-text rationale. The structured output preserved both — `"tools_used": ["mock_news_search (NVDA, volume spike query)", ...]`, `"strengthens": ["[SA-TECH-2] (the move appears to share a common macro driver...)"]`. The schema only declared `array<string>`, so anything string-shaped passed.

2. **Conditional invariants don't render in `model_json_schema()`.** Pydantic's `model_validator(mode="after")` runs in Python; it never appears in the generated JSON Schema. The schema treats `implication`, `strengthens`, `weakens`, `dismissal_reason`, `missing` as five independently-optional fields — the API will accept any combination. The signal/noise/inconclusive invariant is enforced by `AdaptiveBrief.model_validate(...)` post-call, **not** by the schema itself.

3. **Conditional-field null leakage.** Scenario 5 failed Pydantic round-trip. The agent emitted a SIGNAL thread with `"strengthens": ["SA-TECH-2"]` and `"weakens": null` — the AdaptiveBrief SIGNAL invariant requires `weakens` to be a tuple (use `[]` for the empty case), so `None` violates `_assessment_invariant`. The schema's `anyOf [array | null]` is too permissive: it tells the API "either is acceptable" while the Python invariant treats `null` as "field not applicable to this assessment" — which is wrong for the SIGNAL branch. **The failure is non-deterministic at the SIGNAL-with-empty-cross-references branch — observed in 1 of 2 such cases in the spike, indicating it's real and frequent enough to disqualify a vanilla migration.**

   Two viable mitigations:
   * **Prompt-side discipline.** Tell the agent explicitly in the system prompt: "For SIGNAL threads, `strengthens` and `weakens` are arrays — use `[]` for the empty case, never `null`." This is the prompt-level analogue of the current text-format `none` literal.
   * **Schema tightening.** Massage the generated schema into per-Assessment `oneOf` branches that drop the null variant for required-by-assessment fields. Adds ~30 lines to schema generation per agent. Stricter than today's pure Pydantic check.

   Recommend doing both. Prompt discipline is cheap; schema tightening is the belt that catches the suspender slipping.

4. **Tool-call counting needs filtering.** The SDK injects pseudo `ToolUseBlock`s named `"ToolSearch"` and `"StructuredOutput"` for schema lookup and the final structured emission. Scenario 2 reported 4 tool calls; only 2 were real research-tool invocations. The harness's `tool_calls += 1` would over-count by 1–2 per invocation if migrated naively.

## What stays / what changes

| Layer | Today (text mode) | Post-migration (JSON mode) |
|-------|-------------------|----------------------------|
| **Prompt** (adaptive_researcher.md, ~190 lines) | `<output_contract>` template + envelope-discipline rules (no preamble, no parens, exact section markers, …) | Replaced with a one-line "emit JSON conforming to the schema; the API enforces shape." Keeps the conceptual guidance for `Trigger`, `Findings`, `Assessment`, `Confidence`, conditional fields. **~30 lines deleted.** |
| **Harness** (`harness.py:567-594`) | `_collect_response` accumulates TextBlocks; `_build_sdk_options` does not set `output_format` | Add `output_format={"type": "json_schema", "schema": AdaptiveBrief.model_json_schema()}` on options; read `ResultMessage.structured_output` at the end of `_collect_response`; filter `ToolSearch`/`StructuredOutput` from the tool-call counter. **~20 lines edited.** |
| **Parser** (`parser.py`, 626 lines) | Regex-based ladder: fence stripping, header literal, counts-line regex, marker scanning, sector-alias table, bracket-ref extraction, parens-stripping, conditional-field allowlist enforcement | Reduces to a thin `parse_adaptive_brief(payload: dict, *, invocation_id: str) -> AdaptiveBrief`: inject canonical invocation_id, call `AdaptiveBrief.model_validate(payload)`, optionally normalize `tools_used` (strip parens) and `strengthens`/`weakens` (extract bracket contents) before validation if we want to keep tolerating those wire forms. **~580 lines deleted, ~40 lines kept.** |
| **Validator** (`validation.py`, 394 lines) | Layer-2 structural + Layer-3 referential checks against three upstream briefs | **Unchanged.** Cross-document referential resolution (`_check_strengthens_weakens_resolve`, `_build_reference_universe`) is not expressible in JSON Schema. |
| **Tests** | `test_parser.py` (parser-detail tests including the recent regex tolerances), `test_harness.py` (envelope failure modes) | Most `test_parser.py` cases delete (envelope-shape cases moved into `test_models.py` against pre-built dicts); `test_harness.py` adjusts to inject `structured_output` on the fake SDK message stream. |

## Cost-of-migration estimate

| Agent | Schema gen | Harness changes | Prompt rewrite | Test migration | Validator | Estimate |
|-------|-----------|-----------------|----------------|----------------|-----------|----------|
| `adaptive_research` | trivial — model already exists | ~20 lines | ~30 lines deleted | ~880 LOC test deletion + ~100 lines added | unchanged | **1.5 days** |
| `qualitative_research` | model already exists; same shape | ~20 lines | similar | ~470 LOC test deletion + ~100 lines added | unchanged | **1.5 days** |
| `domain_researchers` | parameterized over Sector — schema generation runs once per sector | ~20 lines | ~30 lines deleted (3 sectors share a prompt) | ~760 LOC test deletion + ~80 lines added | unchanged | **1 day** |
| **Total** | | | | | | **~4 days** |

Plus a one-time landing of structural normalization helpers (parens stripping, bracket-ref extraction) lifted out of the legacy parsers — useful for tolerating the wire-shape habits Sonnet has under thinking. ~half day.

**Net code delta:** ≈4,000 LOC of parser-and-test logic deletes (≈1,900 LOC source + ≈2,100 LOC tests); ≈300 LOC of harness/normalization edits land. The verifier scripts and Layer-3 validators are unchanged.

## Cost regression estimate

Spike cost range: $0.063–$0.102 per call across scenarios (variable by scenario complexity). Current production adaptive runs with extended thinking dominate at ~26K output tokens (~$0.40+) before the brief content even matters. The shift from tight text to JSON adds maybe 15–25% on the brief's *output* tokens (syntax overhead) but the **brief is a small fraction of total output** when extended thinking is enabled. Estimated total cost regression: **<5% per call.** Negligible relative to the parser-fix engineering toll.

## Risks and mitigations

1. **Conditional-field null leakage (load-bearing).** As shown in scenario 5: the model emits `null` for conditional fields the assessment branch requires to be present, and the unmodified `model_json_schema()` permits it. Without mitigation, a non-trivial fraction of SIGNAL threads with empty cross-references will fail Pydantic round-trip after migration — turning the brittleness pattern from "envelope" into "post-validate." **Mitigation:** combine prompt discipline ("for SIGNAL threads, use `[]` not `null` for empty cross-references") with schema tightening (per-Assessment `oneOf` branches that drop the null variant for required-by-assessment fields). Both together. Costs ~30 lines per agent.

2. **Schema doesn't enforce string content.** Sonnet still emits `"mock_news_search (NVDA, ...)"` and `"[SA-TECH-2] (free text)"` under provocation. The Layer-3 validator (`_check_tools_used_in_allowlist`, `_check_strengthens_weakens_resolve`) catches both today; under JSON mode the validator catches the same cases. **Mitigation:** lift the existing `_parse_tool_names` / `_parse_references_or_none` helpers out of the legacy parser and run them as a pre-validate normalization step.

3. **Tool-call pseudo-events.** `ResultMessage` arrives carrying `ToolSearch` and `StructuredOutput` ToolUseBlocks the harness will count as research-tool calls. **Mitigation:** filter the counter to only count blocks whose `name` starts with `mcp__alphamind_<server>__`.

4. **Conditional-invariant enforcement is Python-only.** Even with the schema-tightening mitigation in #1, the validator is still in Python. If a future migration to a different runtime drops Pydantic, the API plus the manually-tightened schema together cover most of the invariants. **Mitigation:** non-issue today.

5. **Output verbosity.** JSON is more verbose than tight text. **Mitigation:** monitor first production run; bump `output_token_budget` in `agents.yaml` if needed (the existing 26K budget for adaptive is generous; current production isn't capped on output).

6. **Per-agent rollout risk.** Changing all three at once amplifies blast radius. **Mitigation:** roll out one agent at a time, starting with `adaptive_research` (the freshest brittleness case). Land a feature branch, re-run the verifier suite that produced the 10 fixes, confirm clean before replicating.

## Decision

**GO** with the per-agent rollout described above, **conditional on adopting the conditional-field null mitigation (Risk #1) before migrating the first agent**. Without that mitigation, the migration trades one brittleness pattern for a worse one. With it, the spike's evidence supports a clean migration.

Recommended next steps:

1. Open a follow-up issue for `adaptive_research` migration (1.5 days, plus ~half day for the conditional-field mitigation that's shared across agents). Acceptance criteria:
   - `verify_adaptive_researcher.py` runs clean against live Sonnet without any of the 10 patches needed to land first.
   - 5 consecutive end-to-end runs on the same fixture all pass Pydantic round-trip — confirms the conditional-field mitigation holds under variable thinking budgets.
   - LOC delta within 50% of the estimate; cost regression <10% on the same fixture.
2. After two consecutive clean runs on the migrated adaptive harness, open follow-ups for `qualitative_research` and `domain_researchers`.
3. Keep the legacy parsers and validators on a transition branch; do not delete until two production weeks confirm the new shape.

## Open questions deferred

- The synthesizer harness (`src/alphamind/analysis/synthesizer/harness.py:243-272`) also accumulates TextBlocks rather than reading `structured_output`. The design doc names the synthesizer's output as "Layer 1 JSON-schema by design," yet the harness has the same pattern as the analysis-layer text-format agents — likely a stubbed payload path. Migrating it to `output_format` is a parallel cleanup not in this spike's scope; the same patterns apply.
- The decision-layer agents (analyst, strategist, PM) are stub `__init__.py` packages; their JSON-schema output mode lands when they're implemented and is greenfield (no migration cost).

## References

- Spike script: `scripts/spike_alp288_structured_output.py`
- Spike output: `docs/_archive/spikes/alp-288-spike-output.txt`
- SDK wiring: `claude_agent_sdk/_internal/transport/subprocess_cli.py:371-380`, `claude_agent_sdk/types.py:1091,1737-1743`, `claude_agent_sdk/_internal/message_parser.py:235`
- Adaptive-research artifacts: `src/alphamind/analysis/adaptive_research/{models,parser,harness,validation}.py`, `prompts/analysis/adaptive_researcher.md`
- Recent fixes branch (the 10 patches): uncommitted on `main` at the time of this spike — see `git diff` against `b69940d`
