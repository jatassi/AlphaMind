# LLM output validation approach

Structural validation is how the pipeline process decides whether an LLM agent's response is usable. The runtime policy for what happens *when validation fails* — single corrective retry, context overflow is a hard abort, any agent failure aborts the invocation — is specified in [llm-agent-failure-handling.md](../llm-agent-failure-handling.md). This document specifies the validation mechanism itself: what the pipeline process does to each agent's raw response before handing its output to the next stage, and how that mechanism is tested.

The validator sits at a single seam — immediately after the LLM response returns, before any downstream consumer sees the output. That placement is load-bearing: it is the only point at which the contract between an LLM agent and a deterministic consumer is actually checked, and it is where the distinction between a parseable-but-semantically-wrong output and a structurally malformed one is drawn.

---

## Scope

### In scope

- **Validation stack** — the ordered layers the pipeline process runs against every LLM output (envelope parse, schema validation, referential integrity, stop-reason check), their sequencing, and what each layer can and cannot detect.

- **Per-agent surface mapping** — which agents are validated by a formal JSON Schema (analyst, strategist, PM envelope + embedded OMS commands), which by hand-written structural validators (domain researchers, qualitative researcher, adaptive researcher), and which by stop-reason check only (synthesizer). Rationale for the split and the conditions under which an agent migrates between mechanisms.

- **Reference-ID taxonomy** — the typed prefix conventions (`SA-TECH-N`, `QR-N`, `AR-N`, `CR-N`, `REC-N`, `INV-N`, `SA-N`, `SA-ORD-N`, `BREACH-N`, `ENV-*-N`, `POS-*`, `MON.*`, composite command IDs) consolidated as the single source of truth for the reference-resolution checker. Producer-side format rules and consumer-side resolution rules are distinct concerns named separately.

- **Output parsing and envelope extraction** — how raw SDK response content is turned into the JSON object schema validation runs against. Strict-JSON stance, markdown fence handling, and the cases where parsing itself is the malformed-output detection.

- **Corrective-retry message construction** — the format of the follow-up message sent back to an agent after a schema failure, what the validator surfaces into it, what it deliberately omits, and why the retry is same-context rather than fresh.

- **Unit test plan for the validator** — the validator is itself a deterministic surface. This section names its test concerns, stub boundary, and preliminary unit catalog in the same shape as [unit-test-plan.md](unit-test-plan.md).

### Out of scope (deferred elsewhere)

- **Failure response policy.** Whether a malformed output is retried, whether the retry succeeds or aborts, and what happens after an abort — all of that is [llm-agent-failure-handling.md](../llm-agent-failure-handling.md). This doc is the mechanism; that doc is the policy.

- **Contract content.** What a valid analyst or strategist output *looks like* is in [analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md) and [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md). What a valid PM envelope looks like is in [portfolio-manager.md](../04-decision-layer/portfolio-manager.md). This doc consumes those contracts; it does not define them.

- **LLM reasoning quality.** Whether the content a validator accepts represents *good* analysis is a Phase 4 feedback-loop concern. The validator enforces the contract; a contract-conformant but weak thesis is the evaluation framework's problem, not the validator's.

- **End-to-end propagation.** Whether a malformed output actually produces an aborted invocation in the integration harness is [integration-test-plan.md](integration-test-plan.md)'s failure-mode-propagation section. That plan runs the real validator against scripted malformed fixtures; the unit test plan in this doc tests the validator in isolation against synthetic inputs.

- **Guardrail validation.** The [guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) is a separate mechanism exposed to agents during their own reasoning — structural-output validation and guardrail validation are not the same check. The guardrail tool's outputs *are* validated by the LLM output validator (they populate the `guardrail_validation_result` field in agent outputs), but the tool itself is not in scope here.

---

## Design principles

Five principles applied uniformly across the validator's surfaces.

### The published schema is the contract

Where a formal JSON Schema exists ([analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md), [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md)), the validator runs against that schema directly — no second implementation in code that could drift. Adding a required field in the schema file makes outputs without that field fail validation immediately; removing a field requires a schema revision. This rules out the failure mode where a code-level validator accepts an output the schema says is invalid (or vice versa), which is exactly the drift that erodes the contract's value over time.

For agents without a formal schema, the prose contract in their respective design docs is authoritative; the hand-written structural validator is the code-level expression of that prose. Adding a formal schema for any of those agents in the future supersedes the hand-written validator — the migration direction is one-way.

### Structural, not semantic

The validator checks shape, not meaning. "Did the analyst produce a JSON object matching the analyst schema" is a validator question. "Did the analyst propose a good trade" is not. The boundary matters because a validator that reaches into semantics produces false positives on unfamiliar-but-valid reasoning and is impossible to maintain as prompts evolve. The runtime policy relies on the validator being deterministic and low-drift; semantic checks destroy both properties.

The lone exception is the reference-ID resolution check, which is technically a semantic concern (does this citation resolve to a real brief?) but is load-bearing enough as a malformed-output detection to sit inside the validator rather than downstream. Invented reference IDs appear as plausible-looking strings that the schema cannot distinguish from real ones, and letting them through silently is the structural failure mode the downstream retrieval tool cannot recover from.

### Fail-fast, fail-once

The validator runs in a fixed order: parse → schema → referential → stop-reason. Each layer's failure halts the pipeline through the failure-handling path; the validator does not accumulate errors across layers. This keeps the corrective retry message focused — if schema validation failed, the retry is about the schema; if referential integrity failed *after* schema passed, the retry is about the references. Bundling errors from multiple layers into a single retry message tends to produce LLM responses that fix one layer and break another.

Fail-once also means: once an output fails at a given layer, the same output is not re-checked against the same layer during the retry. The retry produces a new output that runs through all layers fresh.

### Validation lives in the pipeline process

The validator is a module inside the pipeline process — not a separate service, not a daemon, not an adjunct to the continuous monitor. The pipeline process is where the LLM call originates, where the response returns, and where the next-stage consumer runs; validation inherits from that co-location. The continuous monitor does not run LLM calls and has no validation surface. Engine-originated command envelopes do not pass through the LLM output validator because no LLM generated them — they are produced by the continuous monitor and their command IDs are assigned by the OMS command intake layer per [oms-command-ids.md](../oms-command-ids.md).

### Test the validator as a unit

Because the validator sits at a seam every LLM output crosses, its correctness is load-bearing for the entire pipeline. The [unit test plan section](#unit-test-plan) below treats it as a first-class testable surface — boundary cases per layer, adversarial inputs, determinism on identical inputs. Integration tests exercise the validator's *propagation* behavior (does an abort actually abort?); unit tests exercise the validator's *detection* behavior (does it detect this specific malformation?).

---

## Validation stack

Every LLM output passes through four layers in order. Each layer has a single mandate and produces one of three outcomes: pass (proceed to the next layer), fail (halt with a failure classification the policy doc can act on), or context-overflow (a special terminal classification that skips the corrective retry per the runtime spec).

### Layer 1 — Envelope parse

**Mandate:** Turn the SDK response into a single parseable JSON object.

**Inputs:** Raw text content from the agent's response, plus the response metadata (stop reason, token counts).

**Behavior:**

1. Strip a single leading markdown code fence (` ```json`, ` ```, or no fence) and a single trailing fence, if present. Nested fences are a malformed-output indicator — the agent is emitting prose-wrapped JSON or a multi-block response.
2. Attempt to parse the resulting text as a single top-level JSON value via the standard library JSON parser. Exactly one value at the top level is required; trailing tokens after a valid JSON document are a parse failure, as is a response that is entirely prose with no JSON at all.
3. On parse success, hand the parsed object to Layer 2 and attach the raw text to the in-invocation diagnostic record.

**Failure modes detected here:**

- Response is entirely prose with no JSON.
- Response contains prose before and/or after the JSON object ("Here's my analysis: { ... }. Let me know if you need more detail.").
- Response contains multiple top-level JSON values (an array where the schema expects an object, or two concatenated objects).
- Response contains truncated JSON (the stop-reason check in Layer 4 distinguishes truncation from syntactic error — a truncated JSON object here surfaces as a parse error at this layer, and the metadata check in Layer 4 re-classifies it as context-overflow if `stop_reason = max_tokens`).

**Stance:** strict. The validator does not attempt to recover prose-wrapped JSON via regex, does not accept arrays when objects are required, and does not concatenate multi-object responses. Lenient parsing converges on accepting outputs the schema cannot validate; the cost of one corrective retry when an agent emits prose-wrapped JSON is cheaper than the downstream cost of a lenient parser accepting something that later fails semantically.

**Agent-prompting companion:** the system prompts for agents with formal schemas ([prompts/decision/analyst.md](../../../prompts/decision/analyst.md), [prompts/decision/strategist.md](../../../prompts/decision/strategist.md), [prompts/decision/pm.md](../../../prompts/decision/pm.md)) instruct the agent to emit a single JSON object with no surrounding prose. The validator's strict stance and the prompt's instructions are paired — drift on either side is detectable through the schema-failure rate in the feedback loop.

### Layer 2 — Schema validation

**Mandate:** Verify the parsed object against the agent's structural contract.

**Inputs:** Parsed JSON from Layer 1.

**Behavior:**

- **Formal-schema agents (analyst, strategist):** run the parsed object through a JSON Schema Draft 2020-12 validator configured against the published schema file. The validator returns a list of errors, each naming the JSONPath-style location and the violated rule. The first error produced is the validator's "primary" error and is surfaced to the corrective retry; additional errors are attached to the diagnostic record but not included in the retry message (see [corrective-retry construction](#corrective-retry-message-construction) below).
- **Informal-schema agents (domain researchers, qualitative researcher, adaptive researcher):** run the parsed object through the hand-written structural validator for that agent. The structural validator is expressed as a small set of named checks (required sections present, required fields non-empty, enum values from a closed set, sequential integer indexes where required) that collectively encode the prose contract. Error output uses the same shape as the JSON Schema validator — JSONPath-style location, rule name, message — so downstream handling is uniform.

- **Stop-reason-only agents (synthesizer):** the synthesizer produces prose with no producer-side reference IDs of its own — it consumes upstream references but emits none. There is no structural contract to validate at this layer; the synthesizer's output passes Layer 2 unconditionally. Layer 4 (stop-reason check) still applies to detect truncated output. Invented references the synthesizer might emit in its prose are caught downstream when an analyst, strategist, or PM cites them in their own structured outputs and the consumer-side resolution check at Layer 3 fails.

**Failure modes detected here:**

- Missing required field or section.
- Field of wrong type (string where number expected, object where array expected).
- Enum field with a value outside the allowed set.
- Conditional constraint violated (e.g., analyst schema: `entry_window` present but `entry_window_rationale` absent; strategist schema: `thesis_status: invalidated` paired with `recommended_action` other than `close`).
- Pattern-based violations (e.g., `recommendation_id` not matching `^REC-[0-9]+$`).
- Array-length minimums (e.g., `invalidation_legs` empty).

**Schemas used:** the analyst schema is authoritative in [analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md); the strategist schema is authoritative in [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md). When either schema is revised, the revision is the deployment — no separate code update is required to pick up the new rules.

**PM envelope and OMS command schemas:** the PM command envelope has a formal JSON Schema at [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md), covering the two source_provenance variants (`pm_analyst`, `pm_strategist`), the per-source evaluation criterion set (five for pm_analyst, four for pm_strategist), modifications with the `pre_submission` vs. `post_rejection` phase partition, concerns, rationale narrative, and embedded commands. Every OMS command in the envelope's `commands` array validates against [oms-command-schema.md](../05-execution-layer/oms-command-schema.md) — a discriminated union on `command_type` covering OPEN, CLOSE, ADJUST, CANCEL, and ADD with their per-type required fields, command ID format, and thesis-structure requirements. Both schemas run at the LLM output validation seam; the envelope schema's `$ref` to the OMS command schema composes the two. Engine-originated envelopes are specified at [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md) and validated by the OMS command intake layer.

### Layer 3 — Referential integrity

**Mandate:** Verify that references within the output resolve correctly against their expected sources.

**Inputs:** Schema-valid output from Layer 2, plus the invocation-scoped context needed for resolution (synthesizer retrieval store, current portfolio state, active-breach list for the strategist, envelope set for intra-invocation cross-refs).

**Behavior:** the validator applies two distinct classes of reference check.

**Producer-side format checks** — reference IDs the output produces for its own structured elements must follow the documented prefix-and-index rules in the [reference-ID taxonomy](#reference-id-taxonomy) below. These are also enforced by JSON Schema `pattern` constraints where feasible (e.g., `recommendation_id` pattern `^REC-[0-9]+$`), and the referential layer catches cross-element correspondences the schema cannot express:

- Analyst: every `invalidation_rationale[].leg_id` must match an existing `invalidation_legs[].leg_id` in the same recommendation. Sequential indexing (REC-1, REC-2, ... with no gaps) is checked here rather than in the schema.
- Strategist: `action_parameters.action` must equal the outer `recommended_action`. `linked_position_assessment_id` must match an `assessment_id` in the same document. `remedy_flag` must match a `breach_id` in the strategist's guardrail state header. SA-N and SA-ORD-N sequential indexing is checked here.
- Domain researchers: per-section sequential indexing (`SA-{SECTOR}-N` for findings, `SA-{SECTOR}-ANOM-N` for anomalies, `SA-{SECTOR}-TC-N` for thesis candidates) without gaps; unique within section.
- Qualitative researcher: sequential `QR-N` narrative thread indexing; sequential `QR-CW-N` catalyst watch indexing.
- Adaptive researcher: sequential `AR-N` thread indexing.

**Consumer-side resolution checks** — references the output cites to other agents' findings must resolve to real entries:

- Analyst narratives cite `[SA-TECH-3]`, `[QR-4]`, `[AR-2]`, `[CR-1]`-style references embedded in prose fields (`thesis_narrative`, `target_rationale`, `counterarguments_acknowledged`). Every cited ID must exist in the synthesizer's retrieval store for the current invocation. An invented reference ID that is syntactically well-formed but refers to a brief section that does not exist is the primary failure mode here, and surfaces as a structural error at this layer.
- Strategist narratives (`status_rationale`, `action_rationale`, `cross_position_observations`) cite the same synthesizer references; same resolution rule applies.
- PM rationale narratives cite the analyst/strategist recommendations they evaluate by `REC-N` / `SA-N` / `SA-ORD-N`. Every cited ID must exist in the corresponding source output within the same invocation; citations across invocations are a structural error (stale citation) unless the envelope explicitly references prior-invocation activity log entries, which use a distinct citation format documented in [portfolio-manager.md](../04-decision-layer/portfolio-manager.md).
- Engine-originated envelope provenance references to breach records (`rule_breached`, `position_selection_rationale`) must resolve to real rule identifiers from [rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md).

**Position and thesis resolution:** Strategist `position_id` and `thesis_id` fields must match real entries in the portfolio state the pipeline process delivered as context. A `position_id` referring to a position that is not open is a structural error — the strategist cannot act on a position the portfolio state does not show it. PM envelopes referring to `POS-*` identifiers inherit the same rule.

**Scope of resolution:** the validator is not responsible for semantic consistency between a reference and the narrative that cites it (does `[SA-TECH-3]` actually support the claim the narrative attributes to it?). That is the PM's source brief retrieval responsibility, handled during evaluation rather than validation. The validator only asserts the reference resolves — the cited ID points to a real brief section, not an invented one.

**Failure modes detected here:**

- Invented reference ID (syntactically well-formed, does not resolve).
- Mismatched sequential indexing (REC-1, REC-3 with no REC-2).
- Cross-element correspondence failure (invalidation leg without matching rationale entry).
- Foreign-key-style failure (position_id for a non-open position, assessment_id cited in pending_order_assessment that doesn't exist).
- Breach-flag resolution failure (`remedy_flag` citing a `BREACH-N` the guardrail state header didn't surface).

### Layer 4 — Stop-reason check

**Mandate:** Distinguish a syntactically-broken output from a truncated one.

**Inputs:** SDK response metadata — specifically the stop reason.

**Behavior:** if any prior layer failed AND the response metadata indicates the model stopped because it hit the output token ceiling (`max_tokens` or equivalent), the failure is re-classified from `malformed_output` to `context_overflow`. The runtime policy treats the two differently — malformed gets one corrective retry, context overflow is an immediate abort ([llm-agent-failure-handling.md](../llm-agent-failure-handling.md#recovery-semantics)). Without this re-classification, a truncated output would be retried as if the agent had made a recoverable structural mistake, and the retry would produce the same truncation.

**Why the check runs after the structural layers:** a syntactically valid JSON response that hit `max_tokens` is rare but possible (the schema's required fields all emitted before truncation happened, and the truncation occurred in a field the schema does not mark required). Such outputs pass Layers 1–3 and do not need re-classification — they are genuinely valid, and the max-tokens signal is informational rather than error-bearing. Running the check only when prior layers have failed keeps the common path cheap and keeps the classification logic focused on the ambiguous case.

**Out of scope for this layer:** input-side context overflow (prompt too large for the model's window) is caught before the call is made, by the pipeline process's pre-call token-counting check described in [llm-agent-failure-handling.md](../llm-agent-failure-handling.md#detection). The stop-reason check is strictly an output-side classifier.

---

## Per-agent surface mapping

Each LLM agent in the pipeline is validated by the mechanism appropriate to its contract. The table below is the single authoritative mapping; the runtime policy spec ([llm-agent-failure-handling.md](../llm-agent-failure-handling.md)) treats every agent as Critical regardless of which column applies.

| Agent | Validation mechanism | Contract source | Reference IDs produced | References consumed |
|---|---|---|---|---|
| Analyst | JSON Schema Draft 2020-12 | [analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md) | `REC-N`, `INV-N` | `SA-{SECTOR}-*`, `QR-*`, `AR-*`, `CR-*` |
| Strategist | JSON Schema Draft 2020-12 | [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md) | `SA-N`, `SA-ORD-N` | `SA-{SECTOR}-*`, `QR-*`, `AR-*`, `CR-*`, `BREACH-N`, `POS-*` |
| PM | JSON Schema Draft 2020-12 (envelope) + JSON Schema Draft 2020-12 (embedded commands) | [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md), [oms-command-schema.md](../05-execution-layer/oms-command-schema.md) | `ENV-REC-N`, `ENV-SA-N`, `ENV-SA-ORD-N`, composite command IDs (PM-originated form) | `REC-N`, `SA-N`, `SA-ORD-N`, `POS-*` |
| Domain researchers (tech-semis, financials, energy) | Hand-written structural validator | [tech-semis.md §Domain researcher output contract](../03-analysis-layer/domain-researchers/tech-semis.md#domain-researcher-output-contract) | `SA-{SECTOR}-N`, `SA-{SECTOR}-ANOM-N`, `SA-{SECTOR}-TC-N` | — |
| Qualitative researcher | Hand-written structural validator | [qualitative-research.md](../03-analysis-layer/qualitative-research.md) | `QR-N`, `QR-CW-N` | — |
| Adaptive researcher | Hand-written structural validator | [adaptive-research.md](../03-analysis-layer/adaptive-research.md) | `AR-N` | `SA-{SECTOR}-ANOM-*`, `SA-{SECTOR}-*`, `SA-{SECTOR}-TC-*`, `QR-*`, `CR-*` |
| Synthesizer | Stop-reason check only (Layer 4) | [synthesizer.md](../03-analysis-layer/synthesizer.md) | — (no producer-side IDs) | `SA-{SECTOR}-*`, `QR-*`, `AR-*`, `CR-*` (resolution at consumer) |

**Criteria for adopting a formal JSON Schema.** An agent's output warrants a formal schema when (a) the output is consumed by deterministic downstream logic (the proposal pre-processor, the OMS, an engine component), (b) the contract is stable enough that schema revisions are rare, and (c) the validation surface is large enough that a hand-written validator would drift from the prose contract. The analyst, strategist, and PM schemas exist because all three conditions hold — the PM envelope feeds the OMS on every invocation and its evaluation / modification / embedded-command shape is large enough that a hand-written validator would drift from the prose contract over time. The OMS command schema is a shared sub-contract consumed by both the PM envelope schema and the engine envelope schema.

**Criteria for staying structural.** Domain researchers, qualitative researcher, and adaptive researcher all produce narrative briefs that nonetheless carry producer-side reference IDs the synthesizer and decision-layer agents resolve against (`SA-{SECTOR}-N`, `QR-N`, `AR-N`, etc.). Their primary consumer is another LLM, not deterministic logic, but the reference-ID format checks (sequential indexing, prefix correctness, no duplicates) carry most of the structural weight and are uniform across all of them. JSON Schema's value would be weaker than a hand-written validator that focuses on those format checks.

**Criteria for stop-reason-only.** The synthesizer is the only narrative-brief agent that produces no reference IDs of its own — it consumes upstream references but emits none. The producer-side rationale that justifies validators on the other narrative-brief agents does not apply here. Inventing a structural contract for the synthesizer to validate against would be backsolving from the validator's existence rather than from a downstream consumer's need.

---

## Reference-ID taxonomy

Consolidated in one place for the first time. The reference-resolution checker in Layer 3 uses this taxonomy directly; updates to any component of the taxonomy must update this section and the component's source doc simultaneously.

### Producer-side format rules

Each agent produces a bounded set of reference-ID formats:

**Domain researchers** ([domain-researcher output contract](../03-analysis-layer/domain-researchers/tech-semis.md#domain-researcher-output-contract)):
- Findings: `SA-TECH-N` | `SA-FIN-N` | `SA-ENERGY-N` — sector prefix, integer index, sequential within section starting at 1
- Anomalies: `SA-TECH-ANOM-N` | `SA-FIN-ANOM-N` | `SA-ENERGY-ANOM-N` — same sector prefix, `ANOM` segment, integer index, sequential within section
- Thesis candidates: `SA-TECH-TC-N` | `SA-FIN-TC-N` | `SA-ENERGY-TC-N` — same pattern with `TC` segment

**Qualitative researcher** ([qualitative-research.md](../03-analysis-layer/qualitative-research.md)):
- Narrative threads: `QR-N` — integer index, sequential starting at 1
- Catalyst watch: `QR-CW-N` — `CW` segment, integer index, sequential starting at 1

**Adaptive researcher** ([adaptive-research.md](../03-analysis-layer/adaptive-research.md)):
- Investigation threads: `AR-N` — integer index, sequential starting at 1

**Correlation/regime brief** (from the distillation layer; consumed by the synthesizer):
- Findings: `CR-N` — integer index, sequential starting at 1

**Analyst** ([analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md)):
- Recommendations: `REC-N` — sequential within invocation starting at 1
- Invalidation legs: `INV-N` — sequential within recommendation starting at 1

**Strategist** ([strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md)):
- Position assessments: `SA-N` — sequential within invocation starting at 1
- Pending order assessments: `SA-ORD-N` — sequential within invocation starting at 1

**Portfolio manager** ([portfolio-manager.md](../04-decision-layer/portfolio-manager.md)):
- Envelope IDs: `ENV-REC-N` | `ENV-SA-N` | `ENV-SA-ORD-N` — bijective with the source proposal (analyst REC-N → ENV-REC-N, strategist SA-N → ENV-SA-N, strategist SA-ORD-N → ENV-SA-ORD-N)

**Engine (continuous monitor)** ([oms-command-ids.md](../oms-command-ids.md)):
- Engine-originated command IDs: `MON.{session}.{trigger}.{ordinal}` — these do not pass through the LLM output validator (no LLM produces them), but are listed here because the consumer-side resolution rules reference them when an LLM cites engine-originated activity

**Composite command IDs** ([oms-command-ids.md](../oms-command-ids.md)):
- `{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}` — assigned by the OMS command intake layer, not the PM; format-checked when the PM envelope is extracted into commands

**Position identifiers** ([position-model.md](../05-execution-layer/position-model.md)):
- `POS-{ticker}-{NNN}` — not produced by LLM agents, but cited in strategist and PM outputs

**Breach identifiers** (from strategist guardrail state header):
- `BREACH-N` — produced by the guardrail layer when surfacing regime-transition or market-movement breaches to the strategist; cited in strategist `remedy_flag` fields

### Cross-section index uniqueness

Sequential indexes restart per section within a document. `SA-TECH-3` is always the third finding in the tech & semis domain researcher's findings section, never an anomaly (that would be `SA-TECH-ANOM-3`) or a thesis candidate (that would be `SA-TECH-TC-3`). This is what makes the prefix variation load-bearing — the synthesizer's retrieval store indexes by the full prefixed ID, not just the integer, and a missing prefix segment (e.g., a narrative citing `SA-TECH-3` when the intent was `SA-TECH-ANOM-3`) resolves to a different brief section or fails resolution entirely.

### Resolution contexts

The resolution checker loads a different context per agent:

- **Analyst, strategist:** the synthesizer's retrieval store for the current invocation — the authoritative source for all `SA-*`, `QR-*`, `AR-*`, `CR-*` IDs available to the decision layer.
- **Strategist (additional):** the strategist's guardrail state header `BREACH-N` list; the portfolio state's open-position `POS-*` list; the thesis store's thesis IDs.
- **PM:** the analyst output's `REC-N` list and the strategist output's `SA-N` / `SA-ORD-N` lists for the current invocation; the portfolio state's `POS-*` list; the invocation's activity log for prior-invocation citation resolution.
- **Adaptive researcher:** the distillation layer's anomaly flag list and the domain researchers' `SA-{SECTOR}-ANOM-*` set, for `Trigger` field resolution; the domain/qualitative brief set for `Strengthens` / `Weakens` resolution.
- **Domain researchers, qualitative researcher:** no consumer-side references, so resolution is limited to producer-side format checks.

---

## Output parsing and envelope extraction

Layer 1's envelope-parse step is where LLM output becomes structured data. Decisions locked in:

### Single JSON object per response

Every agent emits exactly one JSON object as its response. Multiple objects, arrays at the top level, or prose-wrapped objects all fail parse. This holds for every agent — the output schemas are designed around a single top-level object, and the prompt-side instruction in each agent's system prompt is "emit one JSON object, no prose, no preface, no closing remark." Drift between the prompt instructions and the validator stance is detectable through the malformed-output rate: a spike in parse failures for a specific agent indicates either a prompt issue or a model-behavior regression.

### Markdown fences

A single leading ` ```json` or ` ``` ` fence and a matching trailing ` ``` ` are stripped before parse. Fences are a common mode for Claude to emit structured data, particularly during training-data-era prompts, and stripping them is low-risk. Nested or multiple code fences are treated as a parse failure — the output is emitting prose that happens to contain a JSON block among other content, which is the prose-wrapped case the strict parser is specifically rejecting.

### Trailing whitespace and comments

Whitespace before or after the JSON object is tolerated. JSON does not define comments, and agents occasionally emit JavaScript-style `//` comments inline; these are treated as parse failures because the standard-library parser rejects them. Prompt-side instructions explicitly forbid comments in the output; if the failure rate on this surface grows, the prompt should be tightened before the parser is loosened.

### Diagnostic preservation

Raw text, parsed object (if parse succeeded), and all per-layer error records are attached to the invocation's diagnostic record whether or not the validation ultimately passed. The diagnostic record is the ground truth for the feedback loop's analysis of output-quality trends and for post-hoc investigation of validation failures. It is written regardless of whether the pipeline aborts — this is load-bearing for the no-resume-but-diagnostic-persistence invariant in [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md).

### Stop-reason extraction

The SDK's response metadata is extracted at parse time and preserved on the diagnostic record. Layer 4's stop-reason check reads it from there; if the SDK did not return stop-reason metadata (model API error mid-stream, malformed response object), the classification defaults to `malformed_output` and the corrective retry proceeds — on the reasoning that a response with no metadata is structurally broken at the SDK boundary, which is not the same failure mode as context overflow.

---

## Corrective-retry message construction

When schema validation (Layer 2) or referential integrity (Layer 3) fails, the pipeline process constructs a single corrective follow-up message and re-invokes the agent within the same session. The retry is bounded — one attempt, then abort — and the message must be deliberate about what it says.

### What the message contains

1. **An explicit framing line** naming the validation failure type (schema validation or referential integrity) and the fact that the prior response was rejected. The agent is told its response did not meet the contract, not that its reasoning was wrong.
2. **The primary validator error** — JSONPath-style location, the rule name, the error message. Only the first error is included, not the full error list. Multiple errors surfaced simultaneously tend to produce LLM responses that fix one and break another; a single-error framing produces tight corrections.
3. **A reference to the contract** — the schema's name or the relevant section of the prose contract doc. The agent is not being asked to guess the contract; it is being pointed at the authoritative description.
4. **A directive to produce a single corrected JSON object** with no accompanying prose, following the same contract. The directive repeats the output-parsing expectations from the original prompt so the retry does not introduce envelope drift.

### What the message deliberately omits

- **The full error list.** Multiple simultaneous corrections are the iterative-repair failure mode the runtime policy specifically rules out.
- **Analytical guidance.** The validator does not suggest *what* content should be in the field — only that the field is missing, the enum value is invalid, or the reference is unresolved. Suggesting content biases the agent toward producing plausibly-shaped content that still misses the underlying reasoning; the validator's job ends at "the structural failure is X."
- **The raw input data.** The same-context retry preserves the session's conversation history — the data is still in the agent's context from the initial call. Re-posting it would either duplicate or, if subtly edited, silently change the premise the agent reasoned against.

### Why same-context rather than fresh

Fresh-context retries lose the analytical work that went into the first response. Schema failures rarely indicate the agent's reasoning was wrong — they indicate a formatting or referencing mistake that manifests after the reasoning is complete. Starting fresh biases toward repeating the original framing of the inputs and is more likely to produce the same structural mistake. Same-context retry preserves the reasoning while making the structural failure explicit. This is also why the retry limit is one: the pattern is "give the agent one chance to read its own mistake" rather than an iterative repair loop, which tends to converge on the LLM gaming the schema rather than fixing the underlying problem.

### Why the first retry only

Iterative schema repair produces two failure modes the policy avoids:

- **Schema-gaming convergence.** Over several retries, the agent's output drifts toward the minimum content that passes validation rather than toward fixing the underlying issue. A `thesis_narrative` that gets rejected for missing source references might, on the third retry, acquire a single plausible-looking but invented reference — passing Layer 2 and failing Layer 3.
- **Blocked invocations.** The pipeline is time-bounded by the cadence of scheduled invocations; an iterative repair loop that takes many retries to succeed is functionally equivalent to an abort from the next-invocation's perspective, and the abort path produces cleaner diagnostics.

One retry is the policy. After it, the invocation aborts and the next scheduled trigger produces a fresh invocation against fresh data per the no-checkpoint-no-resume rule in [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md).

---

## Unit test plan

The validator is a first-class deterministic surface. Tests cover each layer independently at the boundaries that matter, plus cross-layer sequencing and the corrective-retry message construction.

### Test concerns

**Layer 1 — Envelope parse.** Strict-parse boundary behavior.

- Valid JSON object with and without leading/trailing whitespace.
- Valid JSON object wrapped in a single `json` code fence, in a single unlabeled code fence, and with no fence.
- Invalid JSON — truncated mid-object, truncated mid-string, missing closing brace.
- Valid JSON preceded by prose, followed by prose, surrounded by prose.
- Multiple concatenated JSON objects ("one" + "two").
- Top-level array where the schema expects an object.
- JavaScript-style comments inside the JSON.
- Nested code fences (fence inside a string value, which the parser should accept, vs. multiple separate fences at the top level, which is a parse failure).
- Empty response body.
- Response containing only whitespace.

**Layer 2 — Schema validation (formal-schema agents).** Each analyst- and strategist-schema constraint is exercised at the boundary.

- Every required field omitted (one test per required field per schema).
- Every enum-valued field tested with a valid value, one invalid value, and the empty string.
- Every conditional constraint exercised in both the satisfied and violated directions (analyst: `entry_window` present without `entry_window_rationale`; strategist: `thesis_status: invalidated` with each non-close action; strategist: `recommended_action: reduce` without `reduce_rationale`; analyst: `target_type: pl_percentage` without `pl_percentage`; etc.).
- Every pattern-constrained field tested at the boundary (valid `REC-1`, invalid `rec-1`, invalid `REC-01`, invalid `REC-` , invalid `RECOMMENDATION-1`).
- Array-length minimums tested at zero, one, and many (analyst `invalidation_legs` empty vs. single-element vs. multi-element; strategist `position_assessments` empty for zero-positions case).
- Mode-conditional requirements exercised (analyst `mode: watchlist` requires `watchlist` array; `mode: normal` requires `recommendations` array; strategist `mode: defensive_posture` requires `defensive_posture_summary`).

**Layer 2 — Schema validation (informal-schema agents).** Each hand-written structural validator is covered by the equivalent boundary tests for its contract:

- Domain researcher validator: valid brief; missing each section header; missing any of the required per-finding fields; invalid signal type / severity / assessment enum; non-sequential indexing (1, 3 without 2); duplicate indexes (two findings both labeled `SA-TECH-2`); more than one catalyst-watch entry when catalyst-watch is bounded.
- Qualitative researcher validator: analogous coverage for `QR-N` and `QR-CW-N`.
- Adaptive researcher validator: analogous coverage for `AR-N`; `Assessment` enum boundary (`signal`/`noise`/`inconclusive`); branch-conditional field requirements (`if signal: Implication required; if noise: Dismissal reason required; if inconclusive: Missing required`).
- PM envelope validator: verdict enum boundary; per-verdict required-field branching (reject has no commands, approve has commands, approve_with_modification has commands and modifications); per-source-provenance required pass/fail keys; OMS command structural checks per extracted command.

The synthesizer has no Layer 2 unit tests — its output passes the layer unconditionally per the Stop-reason-only-agents stance above.

**Layer 3 — Referential integrity.** Each resolution class tested with a valid reference, an invented reference, a syntactically-malformed reference, and a cross-invocation reference (where applicable):

- Analyst consumer-side: every synthesizer-brief reference prefix (`SA-TECH-*`, `SA-FIN-*`, `SA-ENERGY-*`, `QR-*`, `AR-*`, `CR-*`) tested with a present and an invented ID. Narrative containing one valid and one invented reference fails on the invented one.
- Analyst producer-side: `REC-N` sequential index gaps; `INV-N` sequential index gaps; `invalidation_rationale` referring to a `leg_id` not in `invalidation_legs`.
- Strategist consumer-side: same synthesizer-brief coverage; `BREACH-N` resolution against guardrail state header; `position_id` resolution against open-position list; `assessment_id` cross-reference (pending order's `linked_position_assessment_id` must match an existing `SA-N`).
- Strategist producer-side: `SA-N` sequential, `SA-ORD-N` sequential, `action_parameters.action` equals `recommended_action`.
- PM consumer-side: every `REC-N`, `SA-N`, `SA-ORD-N` cited in rationale must exist in the invocation's analyst and strategist outputs.
- PM producer-side: `ENV-REC-N` / `ENV-SA-N` / `ENV-SA-ORD-N` bijection with source proposals; composite command ID format for every extracted command.
- Adaptive researcher: `Trigger`, `Strengthens`, `Weakens` fields resolve against their expected sources.

The synthesizer has no Layer 3 coverage — invented references in synthesizer prose surface at the consumer-side check when an analyst, strategist, or PM cites them in structured output.

**Layer 4 — Stop-reason check.** 

- Valid structural output with `stop_reason: max_tokens` passes (no reclassification because no prior-layer failure).
- Structural failure with `stop_reason: end_turn` remains classified `malformed_output`.
- Structural failure with `stop_reason: max_tokens` reclassifies to `context_overflow`.
- Structural failure with missing stop-reason metadata remains `malformed_output`.

**Cross-layer sequencing.** The validator halts at the first failing layer:

- Layer 1 failure (parse error) does not invoke Layer 2.
- Layer 2 failure (schema) does not invoke Layer 3.
- All layers pass: the output proceeds to the downstream consumer.
- Prior-layer failure paired with `stop_reason: max_tokens`: Layer 4 reclassifies regardless of which prior layer failed (parse, schema, or referential).

**Corrective-retry message construction.**

- The message names the failure type (schema vs. referential) correctly.
- Only the first validator error is included — the message does not expand to accommodate a multi-error list.
- The message references the contract by name (schema file path or contract section), not by embedding the contract inline.
- The message does not contain the raw input data — it relies on same-context session history.
- The message directs the agent to emit a single JSON object — output-parsing expectations are restated.
- Diagnostic record attachment: the offending output, the full error list, and the constructed message are all written to the invocation's diagnostic record regardless of retry outcome.

**Determinism.** Identical inputs produce identical outputs. No clock reads, no RNG, no network access during validation. A validator run is a pure function of (raw response text, response metadata, resolution context) → (outcome, error list, classification).

### Stub boundary

- **Parser:** the standard-library JSON parser is not stubbed — it is the behavior under test. Tests invoke the validator with raw text and assert on classification outcomes.
- **Schema validator library:** the JSON Schema Draft 2020-12 library is treated as trusted third-party code. Tests assert on the validator's *integration* with the library (correct schema file loaded, errors correctly propagated), not on the library's internal behavior.
- **Resolution context fixtures:** the synthesizer retrieval store, portfolio state, breach list, and activity log used for Layer 3 resolution are provided as test fixtures with configurable contents. A fixture builder produces a "current invocation context" with a specified set of valid reference IDs; tests construct malformed outputs that cite valid, invented, or malformed references and assert on the resolution outcomes.
- **SDK response metadata:** stop-reason values are stubbed per test. The SDK itself is not invoked during validator unit tests.
- **Clock:** no clock dependency.

### Preliminary unit catalog

Preliminary. Expect movement as implementation begins; the boundary-class axes are more stable than individual cases.

| Group | Units |
|---|---|
| Envelope parse | single-object parse, code-fence stripping (json-labeled, unlabeled, none), prose-wrapped rejection, multi-object rejection, top-level-array rejection, comment rejection, whitespace tolerance, empty-response classification, stop-reason extraction |
| Analyst schema | required-field coverage (one unit per required field), enum-value boundary (one per enum), conditional-constraint pairs (entry_window, target_type variants, mode variants), pattern-constraint boundaries (REC-N, INV-N), array-length minimums, subschema instrument discriminated-union (equity/option/strategy) |
| Strategist schema | required-field coverage, enum-value boundary, conditional-constraint pairs (thesis_status/recommended_action, action-specific required fields, remedy_flag/remedy_rationale, mode/defensive_posture_summary), pattern-constraint boundaries (SA-N, SA-ORD-N), array-length minimums, subschema action_parameters discriminated-union (close/reduce/adjust-bracket/add) |
| Domain researcher validator | section-header presence, per-finding required fields, signal type enum, severity enum, sequential indexing, duplicate-index rejection |
| Qualitative researcher validator | narrative-thread structure, catalyst-watch structure, sequential indexing |
| Adaptive researcher validator | thread structure, assessment enum, branch-conditional fields, trigger-reference format, strengthens/weakens format |
| PM envelope validator | verdict enum, per-verdict required-field branching, per-source-provenance criterion keys, envelope-ID bijection, commands-list shape |
| OMS command validator | command-type enum, command-type-specific required fields, composite command-ID format, engine-originated `MON.*` format (for engine envelopes extracted by this validator on behalf of the continuous monitor via the shared OMS intake) |
| Reference-ID resolution | valid resolution (one per prefix), invented-ID rejection, malformed-format rejection, sequential-index gap detection, duplicate-index detection, foreign-key resolution (position_id, thesis_id, breach_id) |
| Stop-reason check | success + max_tokens (no reclassification), fail + end_turn (no reclassification), fail + max_tokens (reclassify to context_overflow), fail + missing metadata (no reclassification) |
| Cross-layer sequencing | halt-at-first-failure (one per layer pair), all-layers-pass, Layer 4 reclassification across prior-failure types |
| Corrective retry message | framing line per failure type, single-error inclusion, contract reference, no-raw-data invariant, single-object directive, diagnostic persistence |
| Determinism | pure-function invariant, identical-inputs-identical-outputs sanity |

---

## Shared fixture inventory

The validator's unit tests share fixtures with the broader unit and integration plans; this section adds only the fixtures specific to validation.

### Valid-output fixture per agent

One canonical valid output per agent, constructed to pass every layer. Tests derive malformed fixtures by mutating fields of the canonical output — a library of named mutations (drop a required field, invalidate an enum, insert an invented reference, gap the sequential index) produces the coverage matrix. Mutation-based fixtures beat hand-rolled malformed fixtures on two axes: the valid baseline is the single source of truth for what a conformant output looks like, and the mutation library is reusable across agents with similar schemas.

### Resolution context fixture

Configurable builder for the invocation-scoped resolution context: synthesizer retrieval store (set of valid `SA-*`, `QR-*`, `AR-*`, `CR-*` IDs), portfolio state (set of open `POS-*` IDs and `thesis_id`s), breach list (set of `BREACH-*` IDs), analyst output (set of `REC-*`), strategist output (set of `SA-*`, `SA-ORD-*`), activity log (set of prior-invocation events). Tests construct contexts with specified valid ID sets and assert on resolution outcomes against malformed outputs.

### Schema-repair message fixture

Captured corrective-retry messages for representative failure modes. Tests assert on the message's shape (framing, error reference, contract reference, directive) rather than its exact text, to avoid coupling tests to prompt-phrasing choices that may evolve independently of the validator's logic.

---

## Resolved design questions

Design questions the scoping surfaced, now closed:

- **PM envelope schema formality.** Formal JSON Schema. The PM envelope schema at [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md) covers both pm_analyst and pm_strategist variants; every embedded OMS command validates against [oms-command-schema.md](../05-execution-layer/oms-command-schema.md) via `$ref`. Engine-originated envelopes are a sibling contract at [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md) validated at the OMS command intake layer rather than at the LLM output seam (no LLM produces them).

- **Referential integrity vs. semantic validation.** Referential integrity is in scope (the reference ID resolves) because invented IDs are structurally indistinguishable from real ones at the downstream consumer and fail silently if not caught at the validation seam. Semantic consistency (does the reference actually support the claim?) is out of scope — the PM's source brief retrieval tool is the mechanism for that check, applied during evaluation rather than validation.

- **Corrective retry error granularity.** Single primary error. Surfacing the full error list produces iterative fix-one-break-another cycles; surfacing only the primary error produces tight corrections and a clean policy abort when the retry also fails.

- **Fresh-context vs. same-context retry.** Same-context. The structural failure is downstream of the reasoning in most cases, and fresh context loses the reasoning. The cost of same-context (a slightly larger session on retry) is uniformly cheaper than the cost of fresh-context (lost analytical work, repeated framing mistakes).

- **Validator placement.** Module inside the pipeline process at the LLM invocation seam. No separate service, no adjunct to the continuous monitor (which does not run LLM calls). Engine-originated envelopes bypass the LLM output validator because no LLM generates them — command ID assignment and format checks for those envelopes live at the OMS command intake layer per [oms-command-ids.md](../oms-command-ids.md).

- **Lenient vs. strict parsing.** Strict. Lenient parsing (regex-extracting JSON from prose, accepting multiple top-level objects) converges on accepting outputs the schema cannot validate, and a validator that disagrees with its own parser is worse than either a strict parser or a permissive schema. The prompt instructions pair with the strict parser to produce a stable contract.

- **Stop-reason check ordering.** After the structural layers, not before. Running it first would misclassify valid-but-truncation-compatible outputs (rare but possible) as context overflow; running it only on prior-layer failures keeps the common path cheap and focuses reclassification on the ambiguous case.

---

## Cross-references

- LLM agent failure policy (the runtime response to validator failures): [llm-agent-failure-handling.md](../llm-agent-failure-handling.md)
- Analyst output contract: [analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md)
- Strategist output contract: [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md)
- PM envelope contract (prose): [portfolio-manager.md](../04-decision-layer/portfolio-manager.md)
- PM envelope schema (JSON Schema): [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md)
- Engine-originated envelope schema (JSON Schema; sibling contract, not LLM-validated): [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md)
- OMS command contract (prose): [oms-commands.md](../05-execution-layer/oms-commands.md)
- OMS command schema (JSON Schema): [oms-command-schema.md](../05-execution-layer/oms-command-schema.md)
- OMS command ID discipline: [oms-command-ids.md](../oms-command-ids.md)
- Domain researcher output contract: [tech-semis.md §Domain researcher output contract](../03-analysis-layer/domain-researchers/tech-semis.md#domain-researcher-output-contract)
- Qualitative researcher output: [qualitative-research.md](../03-analysis-layer/qualitative-research.md)
- Adaptive researcher output: [adaptive-research.md](../03-analysis-layer/adaptive-research.md)
- Synthesizer output and reference system: [synthesizer.md](../03-analysis-layer/synthesizer.md)
- Mid-pipeline failure invariants (diagnostic persistence, no-resume): [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md)
- Unit test plan (adjacent deterministic surfaces): [unit-test-plan.md](unit-test-plan.md)
- Integration test plan (validator propagation, not detection): [integration-test-plan.md](integration-test-plan.md)
- LLM integration architecture (Agent SDK, per-agent configuration): [../architecture/llm-integration.md](../../architecture/llm-integration.md)
- Project tracker: [project-tracker.md](../../project-tracker.md)
