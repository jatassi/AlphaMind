# LLM output validation approach

Structural validation decides whether an LLM agent's response is usable. The runtime policy for what happens *when validation fails* — single corrective retry, context overflow is a hard abort, any agent failure aborts the invocation — is specified in [llm-agent-failure-handling.md](../llm-agent-failure-handling.md). This document specifies the validation mechanism itself: what the pipeline does to each agent's raw response before handing output to the next stage, and how that mechanism is tested.

The validator sits at a single seam — immediately after the LLM response returns, before any downstream consumer sees the output. The only point at which the contract between an LLM agent and a deterministic consumer is checked, and where the distinction between parseable-but-semantically-wrong and structurally malformed is drawn.

---

## Scope

### In scope

- **Validation stack** — the ordered layers the pipeline runs against every LLM output (envelope parse, schema validation, referential integrity, stop-reason check), their sequencing, and what each layer can and cannot detect.

- **Per-agent surface mapping** — which agents are validated by formal JSON Schema (analyst, strategist, PM envelope + embedded OMS commands), which by hand-written structural validators (domain researchers, qualitative researcher, adaptive researcher), which by stop-reason check only (synthesizer). Rationale for the split and conditions under which an agent migrates.

- **Reference-ID taxonomy** — typed prefix conventions (`SA-TECH-N`, `QR-N`, `AR-N`, `CR-N`, `REC-N`, `INV-N`, `SA-N`, `SA-ORD-N`, `BREACH-N`, `ENV-*-N`, `POS-*`, `MON.*`, composite command IDs) consolidated as the single source of truth for the reference-resolution checker. Producer-side format rules and consumer-side resolution rules are distinct concerns.

- **Output parsing and envelope extraction** — how raw SDK response content becomes the JSON object schema validation runs against. Strict-JSON stance, markdown fence handling, cases where parsing itself is the malformed-output detection.

- **Corrective-retry message construction** — format of the follow-up message after a schema failure, what the validator surfaces, what it omits, why same-context rather than fresh.

- **Unit test plan for the validator** — boundary cases per layer, adversarial inputs, determinism. Test concerns, stub boundary, and preliminary unit catalog in the same shape as [unit-test-plan.md](unit-test-plan.md).

### Out of scope (deferred elsewhere)

- **Failure response policy.** Whether a malformed output is retried, whether the retry succeeds or aborts, what happens after an abort — [llm-agent-failure-handling.md](../llm-agent-failure-handling.md). This doc is the mechanism; that doc is the policy.

- **Contract content.** What a valid analyst or strategist output *looks like* is in [analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md) and [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md). What a valid PM envelope looks like is in [portfolio-manager.md](../04-decision-layer/portfolio-manager.md). This doc consumes those contracts.

- **LLM reasoning quality.** Whether accepted content represents *good* analysis is a Phase 4 feedback-loop concern. The validator enforces the contract; a contract-conformant but weak thesis is the evaluation framework's problem.

- **End-to-end propagation.** Whether a malformed output actually produces an aborted invocation in the integration harness is [integration-test-plan.md](integration-test-plan.md)'s failure-mode-propagation section. That plan runs the real validator against scripted malformed fixtures; the unit plan here tests the validator in isolation against synthetic inputs.

- **Guardrail validation.** The [guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) is a separate mechanism exposed to agents during reasoning — structural-output validation and guardrail validation are not the same check. The guardrail tool's outputs *are* validated by the LLM output validator (they populate the `guardrail_validation_result` field), but the tool itself is not in scope here.

---

## Design principles

Five principles applied uniformly across the validator's surfaces.

### The published schema is the contract

Where a formal JSON Schema exists ([analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md), [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md)), the validator runs against that schema directly — no second implementation in code that could drift. Adding a required field in the schema file makes outputs without that field fail validation immediately. Rules out the failure mode where a code-level validator accepts an output the schema says is invalid (or vice versa) — the drift that erodes the contract's value over time.

For agents without a formal schema, the prose contract in their design docs is authoritative; the hand-written structural validator is the code-level expression of that prose. Adding a formal schema supersedes the hand-written validator — the migration direction is one-way.

### Structural, not semantic

The validator checks shape, not meaning. "Did the analyst produce a JSON object matching the schema" is a validator question. "Did the analyst propose a good trade" is not. A validator reaching into semantics produces false positives on unfamiliar-but-valid reasoning and is impossible to maintain as prompts evolve. The runtime policy relies on the validator being deterministic and low-drift; semantic checks destroy both properties.

The lone exception is reference-ID resolution, which is technically semantic (does this citation resolve to a real brief?) but is load-bearing enough as malformed-output detection to sit inside the validator. Invented reference IDs appear as plausible-looking strings the schema cannot distinguish from real ones; letting them through silently is the structural failure mode the downstream retrieval tool cannot recover from.

### Fail-fast, fail-once

The validator runs in a fixed order: parse → schema → referential → stop-reason. Each layer's failure halts the pipeline through the failure-handling path; the validator does not accumulate errors across layers. Keeps the corrective retry focused — if schema validation failed, the retry is about the schema; if referential integrity failed *after* schema passed, the retry is about the references. Bundling errors from multiple layers tends to produce LLM responses that fix one and break another.

Fail-once also means once an output fails at a given layer, the same output is not re-checked against the same layer during the retry. The retry produces a new output that runs through all layers fresh.

### Validation lives in the pipeline process

The validator is a module inside the pipeline process — not a separate service. The pipeline is where the LLM call originates, where the response returns, and where the next-stage consumer runs; validation inherits from that co-location. The continuous monitor does not run LLM calls. Engine-originated command envelopes do not pass through the LLM output validator because no LLM generated them — produced by the continuous monitor with IDs assigned by the OMS command intake layer per [oms-command-ids.md](../oms-command-ids.md).

### Test the validator as a unit

Because the validator sits at a seam every LLM output crosses, its correctness is load-bearing. The [unit test plan section](#unit-test-plan) treats it as a first-class testable surface. Integration tests exercise *propagation* (does an abort actually abort?); unit tests exercise *detection* (does it detect this specific malformation?).

---

## Validation stack

Every LLM output passes through four layers in order. Each layer has a single mandate and produces one of three outcomes: pass, fail (halt with a failure classification), or context-overflow (a terminal classification that skips the corrective retry).

### Layer 1 — Envelope parse

**Mandate:** Turn the SDK response into a single parseable JSON object.

**Inputs:** Raw text content from the agent's response, plus response metadata (stop reason, token counts).

**Behavior:**

1. Strip a single leading markdown code fence (` ```json`, ` ```, or no fence) and a single trailing fence. Nested fences are a malformed-output indicator (prose-wrapped JSON or multi-block response).
2. Parse the resulting text as a single top-level JSON value via the standard library parser. Exactly one value required; trailing tokens after a valid JSON document are a parse failure, as is entirely-prose response.
3. On success, hand the parsed object to Layer 2 and attach the raw text to the diagnostic record.

**Failure modes detected here:**

- Response is entirely prose with no JSON.
- Prose before and/or after the JSON object ("Here's my analysis: { ... }. Let me know if you need more detail.").
- Multiple top-level JSON values (an array where the schema expects an object, or two concatenated objects).
- Truncated JSON (Layer 4's stop-reason check re-classifies as context-overflow if `stop_reason = max_tokens`).

**Stance:** strict. The validator does not attempt to recover prose-wrapped JSON via regex, does not accept arrays when objects are required, does not concatenate multi-object responses. Lenient parsing converges on accepting outputs the schema cannot validate; one corrective retry is cheaper than a lenient parser accepting something that later fails semantically.

**Agent-prompting companion:** system prompts for agents with formal schemas ([prompts/decision/analyst.md](../../../prompts/decision/analyst.md), [prompts/decision/strategist.md](../../../prompts/decision/strategist.md), [prompts/decision/pm.md](../../../prompts/decision/pm.md)) instruct the agent to emit a single JSON object with no surrounding prose. Drift on either side is detectable through the schema-failure rate in the feedback loop.

### Layer 2 — Schema validation

**Mandate:** Verify the parsed object against the agent's structural contract.

**Inputs:** Parsed JSON from Layer 1.

**Behavior:**

- **Formal-schema agents (analyst, strategist):** parsed object runs through a JSON Schema Draft 2020-12 validator configured against the published schema. Returns a list of errors, each naming JSONPath location and violated rule. The first error is the "primary" surfaced to the corrective retry; additional errors attach to the diagnostic record but not the retry message (see [corrective-retry construction](#corrective-retry-message-construction)).
- **Informal-schema agents (domain researchers, qualitative researcher, adaptive researcher):** parsed object runs through the hand-written structural validator. Expressed as named checks (required sections present, required fields non-empty, enum values from a closed set, sequential integer indexes) encoding the prose contract. Error output uses the same shape as JSON Schema validator output — JSONPath location, rule name, message.

- **Stop-reason-only agents (synthesizer):** the synthesizer produces prose with no producer-side reference IDs — consumes upstream references but emits none. No structural contract at this layer; passes Layer 2 unconditionally. Layer 4 still applies for truncation. Invented references in synthesizer prose are caught downstream when an analyst, strategist, or PM cites them and Layer 3's consumer-side check fails.

**Failure modes detected here:**

- Missing required field or section.
- Field of wrong type (string where number expected, object where array expected).
- Enum field with a value outside the allowed set.
- Conditional constraint violated (e.g., analyst schema: `entry_window` present but `entry_window_rationale` absent; strategist schema: `thesis_status: invalidated` paired with `recommended_action` other than `close`).
- Pattern-based violations (e.g., `recommendation_id` not matching `^REC-[0-9]+$`).
- Array-length minimums (e.g., `invalidation_legs` empty).

**Schemas used:** analyst schema in [analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md); strategist schema in [strategist-output-schema.md](../04-decision-layer/strategist-output-schema.md). When either schema is revised, the revision is the deployment — no code update required.

**PM envelope and OMS command schemas:** the PM envelope has a formal JSON Schema at [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md), covering the two source_provenance variants (`pm_analyst`, `pm_strategist`), per-source evaluation criteria (five for pm_analyst, four for pm_strategist), modifications with the `pre_submission` vs. `post_rejection` partition, concerns, rationale narrative, and embedded commands. Every OMS command in the envelope's `commands` array validates against [oms-command-schema.md](../05-execution-layer/oms-command-schema.md) — a discriminated union on `command_type` covering OPEN, CLOSE, ADJUST, CANCEL, ADD with per-type required fields, command ID format, thesis-structure requirements. Both run at the LLM output validation seam; the envelope schema's `$ref` to OMS command composes them. Engine-originated envelopes are specified at [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md) and validated by the OMS command intake layer.

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

**Position and thesis resolution:** Strategist `position_id` and `thesis_id` must match real entries in the delivered portfolio state. A `position_id` referring to a position not open is a structural error. PM envelopes referring to `POS-*` inherit the same rule.

**Scope of resolution:** the validator is not responsible for semantic consistency between a reference and the narrative that cites it (does `[SA-TECH-3]` actually support the claim?). That is the PM's source brief retrieval responsibility during evaluation. The validator only asserts the reference resolves — the cited ID points to a real brief section.

**Failure modes detected here:**

- Invented reference ID (syntactically well-formed, does not resolve).
- Mismatched sequential indexing (REC-1, REC-3 with no REC-2).
- Cross-element correspondence failure (invalidation leg without matching rationale entry).
- Foreign-key-style failure (position_id for a non-open position, assessment_id cited in pending_order_assessment that doesn't exist).
- Breach-flag resolution failure (`remedy_flag` citing a `BREACH-N` the guardrail state header didn't surface).

### Layer 4 — Stop-reason check

**Mandate:** Distinguish a syntactically-broken output from a truncated one.

**Inputs:** SDK response metadata — specifically the stop reason.

**Behavior:** if any prior layer failed AND response metadata indicates the model stopped because it hit the output token ceiling (`max_tokens` or equivalent), the failure is re-classified from `malformed_output` to `context_overflow`. Runtime policy treats the two differently — malformed gets one corrective retry, context overflow is immediate abort ([llm-agent-failure-handling.md](../llm-agent-failure-handling.md#recovery-semantics)). Without re-classification, a truncated output would be retried as a recoverable mistake, and the retry would produce the same truncation.

**Why the check runs after structural layers:** a syntactically valid JSON response that hit `max_tokens` is rare but possible (required fields all emitted; truncation in a non-required field). Such outputs pass Layers 1–3 and don't need re-classification. Running the check only on prior-layer failure keeps the common path cheap.

**Out of scope:** input-side context overflow (prompt too large for the model's window) is caught before the call by the pre-call token-counting check ([llm-agent-failure-handling.md](../llm-agent-failure-handling.md#detection)). The stop-reason check is strictly output-side.

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

**Criteria for adopting a formal JSON Schema.** An agent's output warrants a formal schema when (a) consumed by deterministic downstream logic (proposal pre-processor, OMS, engine), (b) the contract is stable enough that schema revisions are rare, and (c) the validation surface is large enough that a hand-written validator would drift. Analyst, strategist, and PM schemas satisfy all three — the PM envelope feeds the OMS on every invocation and its evaluation/modification/embedded-command shape is large enough that hand-written validation would drift over time. The OMS command schema is a shared sub-contract consumed by both the PM envelope schema and the engine envelope schema.

**Criteria for staying structural.** Domain researchers, qualitative researcher, and adaptive researcher produce narrative briefs that nonetheless carry producer-side reference IDs (`SA-{SECTOR}-N`, `QR-N`, `AR-N`, etc.). Primary consumer is another LLM, not deterministic logic; reference-ID format checks (sequential indexing, prefix correctness, no duplicates) carry most of the structural weight. JSON Schema's value would be weaker than a hand-written validator focused on format checks.

**Criteria for stop-reason-only.** The synthesizer produces no reference IDs of its own — consumes upstream references but emits none. The producer-side rationale justifying validators on other narrative agents doesn't apply.

---

## Reference-ID taxonomy

The reference-resolution checker in Layer 3 uses this taxonomy directly; updates to any component must update this section and the component's source doc simultaneously.

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

Sequential indexes restart per section within a document. `SA-TECH-3` is always the third finding in the tech & semis findings section, never an anomaly (`SA-TECH-ANOM-3`) or thesis candidate (`SA-TECH-TC-3`). The prefix variation is load-bearing — the synthesizer's retrieval store indexes by full prefixed ID; a missing prefix segment (citing `SA-TECH-3` when the intent was `SA-TECH-ANOM-3`) resolves to a different brief section or fails entirely.

### Resolution contexts

The resolution checker loads a different context per agent:

- **Analyst, strategist:** the synthesizer's retrieval store for the current invocation — the authoritative source for all `SA-*`, `QR-*`, `AR-*`, `CR-*` IDs available to the decision layer.
- **Strategist (additional):** the strategist's guardrail state header `BREACH-N` list; the portfolio state's open-position `POS-*` list; the thesis store's thesis IDs.
- **PM:** the analyst output's `REC-N` list and the strategist output's `SA-N` / `SA-ORD-N` lists for the current invocation; the portfolio state's `POS-*` list; the invocation's activity log for prior-invocation citation resolution.
- **Adaptive researcher:** the distillation layer's anomaly flag list and the domain researchers' `SA-{SECTOR}-ANOM-*` set, for `Trigger` field resolution; the domain/qualitative brief set for `Strengthens` / `Weakens` resolution.
- **Domain researchers, qualitative researcher:** no consumer-side references, so resolution is limited to producer-side format checks.

---

## Output parsing and envelope extraction

Layer 1's envelope-parse step is where LLM output becomes structured data.

### Single JSON object per response

Every agent emits exactly one JSON object. Multiple objects, top-level arrays, or prose-wrapped objects all fail parse. Output schemas are designed around a single top-level object; system prompts instruct "emit one JSON object, no prose, no preface, no closing remark." Drift between prompt and validator is detectable through the malformed-output rate.

### Markdown fences

A single leading ` ```json` or ` ``` ` fence and a matching trailing ` ``` ` are stripped before parse. Fences are a common Claude mode for structured data; stripping is low-risk. Nested or multiple code fences are treated as parse failure — the prose-wrapped case the strict parser specifically rejects.

### Trailing whitespace and comments

Whitespace before or after the JSON object is tolerated. JSON doesn't define comments; JavaScript-style `//` comments fail parse. Prompts explicitly forbid comments; if the failure rate grows, tighten the prompt rather than loosen the parser.

### Diagnostic preservation

Raw text, parsed object (if parsed), and all per-layer error records attach to the invocation's diagnostic record whether validation passed or not. The diagnostic record is ground truth for the feedback loop and post-hoc investigation. Written regardless of whether the pipeline aborts — load-bearing for the no-resume-but-diagnostic-persistence invariant in [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md).

### Stop-reason extraction

SDK response metadata is extracted at parse time and preserved on the diagnostic record. If the SDK did not return stop-reason metadata (mid-stream API error, malformed response), classification defaults to `malformed_output` and the corrective retry proceeds — a response with no metadata is structurally broken at the SDK boundary, not context overflow.

---

## Corrective-retry message construction

When schema validation (Layer 2) or referential integrity (Layer 3) fails, the pipeline constructs a single corrective follow-up and re-invokes the agent within the same session. Bounded to one attempt, then abort.

### What the message contains

1. **An explicit framing line** naming the validation failure type (schema or referential) and that the prior response was rejected. The agent is told its response did not meet the contract, not that its reasoning was wrong.
2. **The primary validator error** — JSONPath location, rule name, error message. Only the first error, not the full list. Multiple simultaneous errors produce LLM responses that fix one and break another.
3. **A reference to the contract** — schema name or relevant prose section. The agent is pointed at the authoritative description, not asked to guess.
4. **A directive to produce a single corrected JSON object** with no accompanying prose. Repeats the output-parsing expectations so the retry doesn't introduce envelope drift.

### What the message deliberately omits

- **The full error list.** Iterative-repair failure mode.
- **Analytical guidance.** The validator does not suggest *what* content should be in the field — only that it's missing, invalid, or unresolved. Suggesting content biases toward plausibly-shaped content that misses the underlying reasoning.
- **The raw input data.** Same-context retry preserves session conversation history. Re-posting would duplicate or silently change the premise.

### Why same-context rather than fresh

Fresh-context retries lose the analytical work in the first response. Schema failures rarely indicate wrong reasoning — they indicate formatting or referencing mistakes after reasoning is complete. Starting fresh biases toward repeating the original framing and is more likely to produce the same mistake. Same-context preserves the reasoning while making the structural failure explicit. This is why the retry limit is one: "give the agent one chance to read its own mistake," not an iterative loop that converges on schema-gaming.

### Why the first retry only

Iterative schema repair produces two failure modes the policy avoids:

- **Schema-gaming convergence.** Output drifts toward minimum content that passes validation rather than fixing the underlying issue. A `thesis_narrative` rejected for missing references might acquire a plausible-looking but invented reference on the third retry — passing Layer 2 and failing Layer 3.
- **Blocked invocations.** The pipeline is time-bounded by scheduled invocation cadence; an iterative loop that takes many retries is functionally equivalent to an abort from the next invocation's perspective, with cleaner diagnostics from the abort path.

After one retry, the invocation aborts and the next scheduled trigger produces a fresh invocation against fresh data per the no-checkpoint-no-resume rule in [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md).

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

- **PM envelope schema formality.** Formal JSON Schema at [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md), covering both pm_analyst and pm_strategist variants. Embedded OMS commands validate against [oms-command-schema.md](../05-execution-layer/oms-command-schema.md) via `$ref`. Engine-originated envelopes are a sibling contract at [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md) validated at the OMS command intake layer (no LLM produces them).

- **Referential integrity vs. semantic validation.** Referential integrity is in scope because invented IDs are structurally indistinguishable from real ones at the consumer and fail silently if not caught here. Semantic consistency (does the reference support the claim?) is out of scope — the PM's source brief retrieval tool handles that during evaluation.

- **Corrective retry error granularity.** Single primary error. Full lists produce fix-one-break-another cycles.

- **Fresh-context vs. same-context retry.** Same-context. Structural failure is downstream of reasoning in most cases; fresh context loses the reasoning. Cost of same-context (slightly larger session) is cheaper than cost of fresh-context (lost analytical work, repeated framing mistakes).

- **Validator placement.** Module inside the pipeline process at the LLM invocation seam. No separate service, no monitor adjunct (the monitor runs no LLM calls). Engine-originated envelopes bypass the LLM output validator because no LLM generates them — IDs assigned by the OMS command intake layer per [oms-command-ids.md](../oms-command-ids.md).

- **Lenient vs. strict parsing.** Strict. Lenient parsing (regex-extracting JSON from prose, accepting multiple top-level objects) converges on accepting outputs the schema cannot validate. The prompt and strict parser pair to produce a stable contract.

- **Stop-reason check ordering.** After the structural layers. Running first would misclassify valid-but-truncation-compatible outputs (rare but possible) as context overflow.

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
