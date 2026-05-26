# Analyst treats inline brief preview as canonical; doesn't fetch full brief via retrieve_brief

## Symptom

The analyst received an inline `=== SYNTHESIZER BRIEF ===` block containing only the synthesizer's 1-sentence "thinking aloud" preface, interpreted that as evidence the brief was "incomplete," and bailed with an empty recommendations array. It made **0 tool calls** — never invoked `retrieve_brief` despite having the tool available and despite the preface explicitly mentioning load-bearing reference IDs (`[CR-87]`, `[AR-1]`) that warranted retrieval.

The bailout happened to be the right output for this invocation only because a separate guardrail-state bug had set `Per-position max size: $5` (see upcoming Decision-Layer guardrail-divergence issue), which made any sized proposal impossible. Strip that bug out and the analyst would have emitted empty proposals on faulty grounds.

The strategist and PM, looking at the same 1-sentence inline preface, correctly called `retrieve_brief(AR-1)` and produced full analyses. The analyst is alone in treating the preface as the brief itself.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

Analyst input — `decision/analyst/user_message.md` line 38-39:

```
=== SYNTHESIZER BRIEF ===
The most extreme signal in this invocation is the GOOG:META 5.83σ correlation breakdown [CR-87] confirmed by adaptive research as a 26.7pp 20-session structural divergence [AR-1], propagating through 15+ cross-asset pairs — I'll pull portfolio state while processing the full brief.
```

Analyst response — `decision/analyst/response_initial.md` lines 5-6:

> *"The synthesizer brief is incomplete. The provided content is a single introductory sentence ... followed by an authoring aside ('I'll pull portfolio state while processing the full brief'). No causal chain, catalyst timing, price structure, or supporting cross-references are available."*

Progress log — `progress.jsonl`:

```
{"agent": "analyst", "duration_s": 26.64, "input_tokens": 8, "output_tokens": 1614, "stop_reason": "end_turn", "tool_calls": 0}
```

Compare with strategist (`progress.jsonl`):

```
{"agent": "strategist", "duration_s": 304.46, "input_tokens": 8, "output_tokens": 21100, "tool_calls": 3}
```

Strategist response confirms it retrieved the brief: *"The retrieved \[AR-1\] brief changes my GOOGL read: GOOG is +15.89% over 20 sessions while META is -10.79%..."*

The synthesizer publishes the full brief via tool calls (synthesizer made 2 tool calls in this invocation; `metadata.json` shows `tool_calls_used: 2`). The full brief lives in the synthesizer's published artifact, retrievable via `retrieve_brief(ref_id)` per reference ID. The inline `=== SYNTHESIZER BRIEF ===` content is the synthesizer's `response.md` — its preface — not the brief itself.

## Root cause

Two reinforcing problems:

1. **Misleading header**: The `=== SYNTHESIZER BRIEF ===` header is attached to the synthesizer's `response.md` preface, not to the canonical brief. Reasonable inference for any agent reading the input bundle is that this section contains the brief.
2. **Prompt permissiveness**: `prompts/decision/analyst.md` line 96-100 explicitly frames `retrieve_brief` as optional, advises skipping it on "quiet days with broad consensus," and warns against calling "to satisfy curiosity." Combined with the misleading header, the analyst rationally concludes there is no brief to retrieve and no thesis to propose.

The strategist and PM avoid the trap because their prompts and tasks more directly require referencing specific findings — they end up calling `retrieve_brief` for \[AR-1\] in the course of producing position assessments. The analyst's task (drafting new proposals) is more easily abandoned when the apparent brief looks empty.

## Scope

**Layer 1 — Header clarity:**
Rename the inline section header to `=== SYNTHESIZER BRIEF PREVIEW (full brief via retrieve_brief) ===` — or split it into two clearly-labeled sections, one for the synthesizer's preface and one for the brief access pattern. Apply to analyst, strategist, and PM input assemblers.

**Layer 2 — Analyst prompt tightening:**
Update `prompts/decision/analyst.md` tool_policy for `retrieve_brief`:

* Clarify that the inline `=== SYNTHESIZER BRIEF PREVIEW ===` is intentionally a preface and that the full brief lives in tool-retrievable artifacts.
* Require calling `retrieve_brief` for at least the highest-σ / load-bearing reference IDs cited in the preface before concluding "no thesis warranted."
* Keep the existing guidance about not retrieving for curiosity / padding; the new rule is about avoiding the "no brief, no thesis" bailout when the preface clearly references high-magnitude signals.

**Layer 3 (optional) — Brief retrieval convenience:**
Consider whether `retrieve_brief()` (no ref_id) should return a structured summary of all reference IDs in the brief, so the analyst can scan and decide what to drill into without first having to know which IDs to fetch.

## Acceptance criteria

- [ ] The inline brief section header makes it textually obvious that the content is a preview and the full brief is accessed via tool calls.
- [ ] Analyst calls `retrieve_brief` at least once when the preface mentions any reference at ≥4σ (or equivalent high-magnitude threshold) before emitting empty recommendations.
- [ ] In a re-run of inv-20260518T111140Z with the analyst-side guardrail bug separately resolved, the analyst would call `retrieve_brief(CR-87)` and `retrieve_brief(AR-1)`, evaluate the META-locus divergence against the configured universe, and either emit a thesis or document why none qualify — not bail on "brief incomplete."

## Verification

* Re-run debug-e2e with the analyst guardrail bug fixed; confirm analyst `progress.jsonl` shows `tool_calls > 0`.
* Spot-check analyst reasoning output references at least one retrieved brief explicitly.

## Notes

This issue is independent of the analyst-vs-strategist guardrail-view divergence (separate Decision-Layer issue coming) — both bugs co-presented in this invocation but each can be fixed independently. The brief-handling discipline is what would matter in any future invocation where the analyst sees a healthy guardrail state but a preview-only inline brief.
