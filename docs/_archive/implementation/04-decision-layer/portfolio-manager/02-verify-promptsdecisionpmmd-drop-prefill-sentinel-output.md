# 02 — Verify prompts/decision/pm.md (drop prefill, sentinel output)

## Goal

Verify that `prompts/decision/pm.md` matches the design contract in `docs/design/04-decision-layer/portfolio-manager.md` (mandate, evaluation framework, tool policy, anti-pattern catalogue), then apply two pre-resolved edits per the parent issue's decisions (D) and (E): (1) remove the `<output_contract>` "begin with `{`" prefill directive incompatible with JSON-Schema output mode, (2) rewrite `<task>` and `<output_contract>` so they reflect the sentinel-only output stance — envelopes flow through `submit_envelope` tool calls, not the structured payload. Mirrors strategist story 02 ([ALP-302](<https://linear.app/alphamind-jatassi/issue/ALP-302>)).

## Reading

* `prompts/decision/pm.md` — current prompt; the file to edit.
* `docs/design/04-decision-layer/portfolio-manager.md` — authoritative source for mandate, evaluation framework, anti-patterns, existing-position guidance, synchronous-feedback semantics; the prompt's content must mirror this design.
* `docs/design/04-decision-layer/pm-envelope-schema.md` — authoritative envelope contract; the prompt's references to envelope shape align with this schema.
* `docs/design/04-decision-layer/submit-envelope-tool-schema.md` — `submit_envelope` tool's input/response contract; the prompt's `<tool_policy>` for this tool aligns with this schema.
* `prompts/decision/analyst.md` and `prompts/decision/strategist.md` — sibling prompts; the structural shape this prompt mirrors. Strategist's prompt (post-2026-05-05 <issue id="4e2b26ba-c68b-41e7-a80d-4226f2c864e1">ALP-311</issue>/312 edits) is the closest analogue.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decisions (D) and (E) name the specific edits.
* Memory `feedback_prompt_output_format_compat` — the rationale for removing the prefill directive (incompatible with `output_format = json_schema`; hangs the model in extended thinking).

## Depends on

None — Wave 1 dispatch.

## Scope

In scope: `prompts/decision/pm.md` (prompt edits) and `tests/config/test_pm_prompt.py` (verification test asserting the prompt's structural shape after edits). No code under `src/alphamind/decision/portfolio_manager/`.

### 1\. Verify structural sections

Read the current `prompts/decision/pm.md` and confirm the following sections exist with content matching the design:

* `<role>` — names the PM as the skeptical gatekeeper.
* `<operating_context>` — names fresh-context invocations, the four input shapes (header, pre-processor bundle, synthesizer brief, portfolio state), the four tool names, the halt + emergency mode signals.
* `<inputs>` — names the input order matching the assembler in story 04.
* `<task>` — describes per-proposal envelope production + submission.
* `<method>` — names the per-criterion evaluation flow + cure-vs-reject + envelope set scan + sequencing planning.
* `<tool_policy>` — names the four tools' usage rules.
* `<output_contract>` — describes the envelope shape (currently includes the prefill directive — to be removed).
* `<example_output>` — at least one realistic example.
* `<constraints>` — names the hard rules (every proposal gets an envelope, no thesis rewriting, no action-type replacement, no inventing reference IDs, halt-mode constraints, etc.).

If any section is *substantively* missing (not "could be longer" — actually missing the contract piece named in the design), surface to the operator per the parent issue's surfacing condition for verification stories.

### 2\. Edit (E) — Remove the "begin with `{`" prefill directive

Locate the `<output_contract>` section's directive (currently around line 111: "Begin your response with `{` and emit no prose before or after. Do not wrap the JSON in markdown fences."). **Delete this directive.** Per memory `feedback_prompt_output_format_compat`, prefill directives hang the model under JSON-Schema output mode (the failure mode hit twice in analyst and strategist work trees). The harness will use `output_format = {"type": "json_schema", "schema": PMCompletionRecord.model_json_schema()}`; the schema enforces shape post-generation; prefill is unnecessary and harmful.

### 3\. Edit (D) — Rewrite `<task>` and `<output_contract>` for sentinel-only output

Per parent decision (D), the PM's structured output is a thin completion sentinel (`PMCompletionRecord` — `invocation_id`, `timestamp`, `envelopes_submitted`, `verdict_summary`); envelopes flow through `submit_envelope` tool calls.

Edit `<task>` to read approximately:

> Produce one command envelope per received proposal. Submit each envelope through the `submit_envelope` tool — that tool delivers the envelope to the engine, returns synchronous accept/reject feedback per embedded command, and is the channel through which envelopes are persisted. After all envelopes are submitted (and any post-rejection modifications resubmitted), emit your final structured-output sentinel summarizing the invocation: `invocation_id`, `timestamp`, total envelopes submitted, and the verdict count by category (`approve`, `approve_with_modification`, `reject`).
>
> The submission of an envelope through `submit_envelope` is the act that records the decision. Your final structured output is a completion sentinel; the engine reads each envelope as it submits, not as a batch from your structured payload.

Edit `<output_contract>` to read approximately:

> Your final structured output is a `PMCompletionRecord`:
>
> * `invocation_id` (string) — verbatim from the guardrail header.
> * `timestamp` (ISO 8601) — when you finalized the invocation (after the last envelope's submission resolved).
> * `envelopes_submitted` (integer) — total number of envelopes you submitted via `submit_envelope`. Includes rejection envelopes (which carry zero commands) and post-rejection re-submissions of the same envelope (count the envelope once per finalized state, not per `submit_envelope` call).
> * `verdict_summary` (object) — keys `approve`, `approve_with_modification`, `reject`; values are non-negative integer counts. The sum equals `envelopes_submitted`.
>
> Do not emit envelopes in the structured output; envelopes flow through `submit_envelope` calls. Do not emit prose before or after the structured-output JSON; the harness reads only the structured payload.

Update the `<example_output>` section accordingly: the current top-level wrapper with embedded `envelopes` array is replaced with the sentinel + a narrative stanza describing the tool-call flow that produced the counts. Keep the existing detailed envelope example as **inline within a** `<tool_call>` example showing what one `submit_envelope` invocation looks like (matching the input schema).

### 4\. Update `<tool_policy>` for `submit_envelope`

The current prompt covers `submit_envelope` under "Command submission" with text directing the PM to "Submit commands one at a time". This is now wrong — the tool takes one envelope (which may carry zero or more commands), not one command. Update the section to:

> `submit_envelope(envelope)`:
>
> * Submit each finalized envelope through this tool. The tool returns a `submission_results` array — one result per embedded command — synchronously within the same invocation.
> * On a result with `status: accepted`: proceed to the next envelope.
> * On a result with `status: rejected`: read the `rejection_payload`'s `rules_breached`, `current`, `limit`, `overage`, and `suggested_modification` fields. Update the originating envelope by appending a `modifications[]` entry with `phase: post_rejection`, `adjustment_category: guardrail_rejection_response`, and `triggering_rule` set to the breached rule name. Then either reissue the envelope through `submit_envelope` with revised parameters, skip the failed command and re-submit the envelope without it, or re-evaluate subsequent envelopes in light of the tighter state.
> * Submit envelopes one at a time. Cumulative state from prior accepted submissions is reflected in the next call's `validate_guardrail` and `submit_envelope` evaluations automatically.
> * Stop submitting when all envelopes are submitted or when further submission would contradict your updated read of capital/exposure state. The tool returns the same envelope-id echo regardless of result; use that to correlate the response.

### 5\. Verify the constraints section

The existing `<constraints>` section names "Halt mode (`Mode: HALT` …): do not emit commands of type `OPEN` or `ADD`." This is correct and stays. The other constraints (no thesis rewriting, no action-type replacement, no inventing reference IDs, anti-pattern canonical strings) are correct and stay.

Remove the constraint "Stop after all envelopes are emitted and all submitted commands have received synchronous acknowledgments or rejections. Do not emit prose before, after, or within the JSON object." — the first half is now restated under `<task>` and `<tool_policy>`; the second half (no prose) is implicit in the JSON-Schema mode and was the harmful prefill directive.

### 6\. Add or update the verification test

Under `tests/config/`, ensure a test exists asserting the prompt's structural shape after edits. Mirror `tests/config/test_strategist_prompt.py` (the strategist prompt's verification test from <issue id="9e92a240-d117-483c-9201-a3551467d5f3">ALP-302</issue>).

The test must:

* Load `prompts/decision/pm.md` as text.
* Assert each of the eight named sections exists by `<section_name>` ... `</section_name>` substring presence.
* Assert the prefill directive substrings are absent (`'Begin your response with `{`'` and `'Do not wrap the JSON in markdown fences'`).
* Assert the sentinel-shape token `PMCompletionRecord` is mentioned in `<output_contract>`.
* Assert the `submit_envelope` tool's "envelope" wording (not "command") is in `<tool_policy>`.

### Out of scope

* Editing any other prompt file.
* Implementing the harness, parser, validator, or runner — those are downstream stories.
* Verifying the eventual harness's behavior — story 09 owns end-to-end live-SDK verification.

## Acceptance criteria

- [ ] `prompts/decision/pm.md` no longer contains "Begin your response with `{`" or "Do not wrap the JSON in markdown fences".
- [ ] `prompts/decision/pm.md`'s `<task>` section names "submit each envelope through the `submit_envelope` tool" and the completion-sentinel emission.
- [ ] `prompts/decision/pm.md`'s `<output_contract>` section names `PMCompletionRecord` with the four fields (invocation_id, timestamp, envelopes_submitted, verdict_summary).
- [ ] `prompts/decision/pm.md`'s `<tool_policy>` section's `submit_envelope` block uses "envelope" (not "command") as the unit of submission.
- [ ] `prompts/decision/pm.md`'s `<example_output>` is updated to show the sentinel shape; the detailed envelope example survives as an inline tool-call example.
- [ ] `prompts/decision/pm.md`'s `<constraints>` section retains halt-mode + no-thesis-rewriting + no-action-replacement + no-inventing-references + anti-pattern canonical-string rules.
- [ ] A test under `tests/config/test_pm_prompt.py` (or sibling) asserts the structural shape after edits.
- [ ] `uv run pytest tests/config/ -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass without changes.

## Verification

Run `uv run pytest tests/config/ -n auto` — the PM-prompt structural test passes. Open `prompts/decision/pm.md` and confirm: (1) the `<output_contract>` section reads as the sentinel description, (2) the prefill directive is gone, (3) the `<task>` and `<tool_policy>` sections frame envelope-as-unit-of-submission. The diff against the pre-edit prompt is concentrated in those three sections plus `<example_output>`.
