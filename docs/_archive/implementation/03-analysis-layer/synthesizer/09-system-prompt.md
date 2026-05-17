# 09 — Verify system prompt

## Goal

Round-trip the existing synthesizer system prompt at `prompts/analysis/synthesizer.md` against the design doc and the `agent-system-prompts` skill's quality bar. The prompt was authored 2026-04-27 during the configuration-management work tree's "Land analysis-layer prompts" quick win (see `project-tracker.md`). This story confirms it covers every contract the synthesizer harness (08) and runner (10) build against, and surfaces any gap to the operator before downstream stories dispatch.

## Reading

* `prompts/analysis/synthesizer.md` — the existing prompt (133 lines, \~14.8 KB).
* `docs/design/03-analysis-layer/synthesizer.md` — the design contract the prompt implements (role, inputs, source-reference mechanism, contradiction handling, output, retrieval-store side-effect, portfolio-state tools, anti-patterns).
* `docs/design/testing/llm-output-validation.md` — reference-ID taxonomy plus Layer-4 stop-reason posture; the prompt's citation discipline must match.
* `prompts/analysis/tech_semis_researcher.md`, `prompts/analysis/qualitative_researcher.md`, `prompts/analysis/adaptive_researcher.md` — sibling prompts; the synthesizer's prompt should be at the same readiness level (role, operating context, inputs, task, method, tool policy, output contract, worked example, constraints).
* `.claude/skills/agent-system-prompts/SKILL.md` — the skill that drives the verification.
* [ALP-208](https://linear.app/alphamind-jatassi/issue/ALP-208) (story 08) — the harness that loads this prompt path; the prompt must be free of stale path references that confuse the harness diagnostic logs.

## Depends on

Nothing — verification.

## Scope

Run the `agent-system-prompts` skill against the existing prompt and confirm coverage of every section listed below. Each section must be present and substantively complete.

#### Required prompt sections

* **Role** — one paragraph anchored on "synthesizer in a systematic trading pipeline."
* **Operating context** — fresh-context-window note, input order, output consumer, citation prefix taxonomy, available tools, no-trade-recommendation constraint.
* **Inputs** — volatility regime label first, six brief sources in fixed order with prefixes (`CR`, `SA-TECH`, `SA-FIN`, `SA-ENERGY`, `QR` plus `QR-CW`, `AR`), three portfolio-state tools.
* **Task** — three signal classes (intersections, contradictions, uncertainty) plus optional portfolio cross-references.
* **Method** — step-by-step reading order; intersection/contradiction/uncertainty discipline.
* **Tool policy** — explicit "do not call when..." guardrails for each portfolio tool, default-zero stance.
* **Output contract** — prose, no JSON, no producer-side reference IDs of the synthesizer's own, citation format `[<prefix>-<index>]`, no closing summary.
* **Worked example** — at least one fully-fleshed example showing the synthesis form.
* **Constraints** — anti-patterns covered (`forced_resolution`, `uncited_claim`, `invented_reference`, `narrative_padding`, `thesis_generation`, `thesis_status_classification`, `tool_call_for_no_reason`, `confidence_inflation`, `summary_at_end`).

#### Mechanical edits

The comment block at the top of the prompt currently references stale `docs/implementation/...` paths. Replace those references with the corresponding Linear issue links (ALP-207 for the input-bundle assembler, ALP-206 for portfolio MCP tools), or remove the comment block entirely.

#### Surfacing condition

If the verification surfaces a substantive gap (missing section, contract drift), pause and surface to the operator before editing the prompt body. Do not rewrite prose unilaterally.

## Out of scope

* Rewriting the prompt body unless verification surfaces a substantive gap and the operator confirms.
* Authoring a new prompt from scratch (the existing prompt is substantially complete).
* Modifying `agents.yaml` (story 02 covers verification of the entry pointing at this file).

## Acceptance criteria

- [ ] `agent-system-prompts` skill verification produces no substantive findings, OR the operator confirms each surfaced gap before any body edit.
- [ ] Comment-block doc-path references at the top of the prompt point to Linear issues, not archived `docs/implementation/...` paths.
- [ ] The prompt's `<inputs>` block prefix list contains no `DC` reference.
- [ ] No regression in the prompt's `<example_output>` block.
- [ ] If any edit lands, `uv run ruff check . && uv run ruff format .` pass (mypy/pytest unaffected — prompt file is markdown).