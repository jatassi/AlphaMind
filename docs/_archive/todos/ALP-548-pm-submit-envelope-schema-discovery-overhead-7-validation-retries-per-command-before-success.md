# PM submit_envelope schema discovery overhead — 7 validation retries per command before success

## Symptom

The PM portfolio manager spent 7 schema-validation retries on its first envelope before producing a successfully-accepted submission. Each retry burned tokens and wall-clock latency. The same iterative-discovery pattern is likely to recur on every invocation that includes a CLOSE with `close_rationale_type=risk_management`, because the schema's conditional required-field constraint isn't surfaced in the PM's prompt or in the MCP tool's input-schema description.

The same convergence pattern is observable in `failed_submission_log.json` — each failure reveals one additional constraint the model didn't know about, and the model retries with a correction that exposes the next constraint, etc.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`decision/portfolio_manager/failed_submission_log.json` records 7 failures before the first ENV-SA-1 envelope succeeded. The error progression:

| Attempt | Validation error revealed |
| -- | -- |
| 1 | `Unable to extract tag using discriminator 'source_provenance'` — sent `{}` (warmup probe) |
| 2 | `commands.0.close.order_type` field required; `execution_method` extra; `close_rationale_type` field required; `close_rationale` extra |
| 3 | `commands.0.close.close_rationale_type` field required; `close_rationale` extra (wrong nesting) |
| 4 | `commands.0.close.close_rationale_type` field required; `close_rationale` extra (still wrong nesting) |
| 5 | `commands.0.close.close_rationale` extra (kept stray field) |
| 6 | `commands.0.close` value error: `close_rationale_type=risk_management requires risk_management_subtype` |
| 7 | `commands.0.close.risk_management_subtype` literal error: `'size_cure'` not in `['pm_directed', 'engine_guardrail']` |
| 8 (success) | Used `risk_management_subtype: pm_directed` |

The model spent 6 corrections + 1 successful attempt to discover:

1. Use `order_type` directly, not `execution_method` wrapping.
2. Use `close_rationale_type` at command level (not inside a `close_rationale` object).
3. Don't send a `close_rationale` narrative field at all (it's not in the schema).
4. When `close_rationale_type=risk_management`, also include `risk_management_subtype`.
5. `risk_management_subtype` only allows `pm_directed` or `engine_guardrail`; `size_cure` (the natural label for the use case) is not allowed.

## Root cause

Three reinforcing problems:

1. **Schema constraint not surfaced in prompt.** `prompts/decision/pm.md` line 103-108 documents `submit_envelope` at a behavioral level (what to do on accept/reject) but does not document the envelope field requirements. The PM is expected to follow the schema in `docs/design/04-decision-layer/pm-envelope-schema.md`, but that file's conditional rule (lines 124-140) requires the PM to internalize a nested JSON-schema `oneOf` to know that risk_management closes need risk_management_subtype.
2. **MCP tool input schema may not encode conditional requirements.** The MCP `submit_envelope` tool's input schema (visible to the model when it composes a tool call) likely declares all fields with their base types but doesn't encode the conditional "if close_rationale_type=risk_management, then risk_management_subtype is required." Pydantic catches the violation at validation time, but the model sees the constraint only via failure feedback.
3. `size_cure` **vocabulary mismatch.** The PM's natural label for the use case ("I'm reducing a position to clear a per-position size cap") would be `size_cure`. The schema only allows `pm_directed` or `engine_guardrail`. The model invented a more accurate label, paid the validation cost, and converged on a less-accurate one. This is a vocabulary-design issue: the schema's allowed values don't cover the most common PM-driven reduction case with a precise label.

## Scope

**Layer 1 — PM prompt: surface the schema constraints.**
Update `prompts/decision/pm.md` `submit_envelope` tool_policy section to include, inline:

* The required-when-condition rule for `risk_management_subtype`.
* The complete enum of allowed `risk_management_subtype` values.
* The complete enum of allowed `close_rationale_type` values.
* The shape of an example successful envelope for a CLOSE command (one canonical good example).

This duplicates information from the schema doc, but eliminates the conditional-discovery loop.

**Layer 2 — Schema vocabulary: add** `size_cure` **(or document the mapping).**
The most common PM-driven CLOSE in this invocation type is a strategist-recommended `reduce` to bring a position under the per-position size cap. Either:

* Add `size_cure` as an allowed `risk_management_subtype` value.
* Or document in the prompt that `pm_directed` is the umbrella label for "size cure," "thesis tightening," and other PM-driven reductions, with examples.

`size_cure` is the more informative choice — the feedback loop can aggregate "PM did N size cures this week" cleanly with a distinct label.

**Layer 3 — MCP tool input schema (optional, larger lift).**
If the MCP tool's input schema can encode JSON-schema `oneOf` / conditional `required` blocks, surface the conditional rules there so the model sees them during composition rather than via validation feedback. Likely requires regenerating the input schema from the Pydantic model with conditional support.

## Acceptance criteria

- [ ] First-call submit_envelope success rate for the canonical PM-directed CLOSE pattern is ≥80% (vs 0% in this invocation — first success was attempt 8).
- [ ] Total schema-validation retries per invocation drops materially (target: ≤2 retries even on novel envelope shapes).
- [ ] PM prompt's `submit_envelope` tool_policy section documents the close_rationale_type enum, the risk_management_subtype enum, and the conditional required-field rule inline.
- [ ] One canonical example envelope for a CLOSE command lives in the prompt.

## Verification

* Re-run debug-e2e; count entries in `failed_submission_log.json`. Expect 0-2 entries instead of 7.
* Confirm the first ENV-SA-1 envelope succeeds on attempt 1 or 2.

## Notes

Independent of all data freshness / distillation / brief-handling issues. Lives in the prompt + schema boundary. Lower-priority than the data-correctness bugs because the PM always converges (no incorrect submissions land), but every invocation pays the discovery cost — token + latency tax compounds across the operational fleet.
