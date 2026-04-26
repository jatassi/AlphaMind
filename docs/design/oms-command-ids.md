# OMS command IDs

Deterministic identifiers for every OMS command. This document specifies the naming discipline — generation, format, uniqueness guarantees, and error handling for duplicates. The purpose is traceability and audit-log correlation, not network-style deduplication; the AlphaMind architecture rules out the scenarios that would require a dedup mechanism (see [§Why not dedup](#why-not-dedup) below).

---

## Scope

Covers command ID generation for the two command origins defined in [oms-commands.md §Command origins](05-execution-layer/oms-commands.md):

- **PM-originated commands** — extracted from envelopes produced by the portfolio manager during Phase 2 of a pipeline invocation.
- **Engine-originated commands** — CLOSE commands issued by the continuous monitor between invocations via engine-originated envelopes.

Related but out of scope: broker submission retry semantics and the surfacing of failed submissions to the originating agent — see [state-persistence.md § Phase 2 write path](05-execution-layer/state-persistence.md).

---

## PM-originated command IDs

The command ID is derived by the OMS command intake layer at envelope receipt time from the envelope's structural position. The PM never generates, reads, or reasons about command IDs — it produces envelopes, and infrastructure assigns identifiers. This parallels the existing principle in [oms-commands.md](05-execution-layer/oms-commands.md) that prevents LLMs from providing values they would hallucinate (the guardrail layer computes greeks internally rather than accepting them from the PM; the same logic applies to IDs).

### Format

```
{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}
```

- **`invocation_id`** — assigned by the pipeline process at invocation start, monotonically increasing ([state-persistence.md §Invocation records](05-execution-layer/state-persistence.md)).
- **`envelope_id`** — deterministically derived from the source proposal the envelope evaluates: `ENV-<source_id>`. Pattern: `^ENV-(REC|SA|SA-ORD)-[0-9]+$`.
  - `ENV-REC-<n>` for envelopes evaluating analyst recommendations (source pattern `^REC-[0-9]+$`, see [analyst-output-schema.md](04-decision-layer/analyst-output-schema.md)).
  - `ENV-SA-<n>` for envelopes evaluating strategist per-position assessments (source pattern `^SA-[0-9]+$`, see [strategist-output-schema.md](04-decision-layer/strategist-output-schema.md)).
  - `ENV-SA-ORD-<n>` for envelopes evaluating strategist pending-order assessments (source pattern `^SA-ORD-[0-9]+$`).
  - The three source-ID prefixes are disjoint, so the envelope ID is bijective with its source.
- **`command_ordinal`** — 0-indexed position within the envelope's embedded commands array. In the steady-state design every envelope produces at most one command, so this is almost always `0`; the field preserves optionality without hidden coupling.
- **`attempt_seq`** — computed by the OMS at receipt as the count of post-rejection modifications in the envelope's modifications array (see [§attempt_seq computation](#attempt_seq-computation) below). First submission is `0`; each successive guardrail-rejection-driven modification increments the counter.

### `attempt_seq` computation

The envelope's `modifications` array contains entries produced in two distinct phases:

- **Pre-submission modifications** (e.g., `conviction_disagreement`, `sizing_adjustment`) — applied by the PM at envelope authoring time, baked into the envelope's initial state before the first submission. Do not affect `attempt_seq`.
- **Post-rejection modifications** (`guardrail_rejection_response`) — appended after a synchronous guardrail rejection from the OMS. Each entry increments `attempt_seq` by one.

Computation: `attempt_seq = count(modifications where phase == "post_rejection")`. The envelope carries its own attempt history in a single structured array; no side state is required.

The envelope modification schema structurally partitions these two phases via an explicit `phase: "pre_submission" | "post_rejection"` enum on each modification entry (rather than relying on category-name convention). The partition is formalized in [pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md) — `guardrail_rejection_response` entries are always `post_rejection`; all other categories are always `pre_submission`.

### Retry and modification model

Each retry of an envelope is a *new* command with a new ID, not a reuse of the prior ID:

- **Modified retry after guardrail rejection.** PM appends a `guardrail_rejection_response` entry to the envelope's modifications, revises the embedded command (e.g., smaller size, different strike), and the pipeline process resubmits. The OMS sees one more `post_rejection` entry → `attempt_seq` increments → new `command_id`. Treated as a new command; validated against fresh state.
- **Same-content retry.** Not an intentional part of this design. Under the AlphaMind architecture (in-process pipeline and OMS, atomic transactions, fail-closed mid-pipeline policy), no pathway produces a same-ID resubmission with identical content — see [§Why not dedup](#why-not-dedup).

### Worked example

Invocation `inv-2026-04-23T14-30Z`. Analyst produces `REC-2`; PM emits envelope `ENV-REC-2` evaluating it, with a pre-submission `conviction_disagreement` reducing size from 4% to 3%.

1. Pipeline process hands `ENV-REC-2` to the OMS. OMS counts post-rejection modifications: 0 → `attempt_seq = 0`. Derives `inv-2026-04-23T14-30Z.ENV-REC-2.0.0`.
2. Guardrail rejects (sector concentration shifted during PM evaluation). OMS writes `guardrail_rejection` activity log entry, returns rejection payload synchronously.
3. PM appends `{phase: "post_rejection", category: "guardrail_rejection_response", ...}` to `ENV-REC-2.modifications`, revises the embedded OPEN to 1.5%.
4. Pipeline process resubmits. OMS counts post-rejection modifications: 1 → `attempt_seq = 1`. Derives `inv-2026-04-23T14-30Z.ENV-REC-2.0.1`. Different command from step 1 — different ID, different content, validated fresh.

If step 4 succeeds, activity log reflects both attempts: the rejected first attempt and the approved second. If step 4 is itself rejected, `attempt_seq` goes to 2, and so on. Each attempt is a first-class command for audit and feedback-loop purposes.

---

## Engine-originated command IDs

Parallel scheme, scoped to continuous monitor sessions rather than pipeline invocations.

### Format

```
MON.{monitor_session_id}.{trigger_id}.{command_ordinal}
```

- **`MON` prefix** — disambiguates engine-originated from PM-originated (which begin with `inv-…`).
- **`monitor_session_id`** — unique per monitor process lifetime. Generated at session start (UUID or start-timestamp). A monitor restart creates a new session, so a re-detected breach after restart produces a new ID.
- **`trigger_id`** — monotonically increasing within a monitor session. Assigned when the monitor records a breach trigger (see [architecture.md §4b](05-execution-layer/architecture.md)).
- **`command_ordinal`** — 0-indexed within the trigger. In practice `0` always: per [architecture.md §4b](05-execution-layer/architecture.md), a trigger produces a single CLOSE.

### No `attempt_seq`

The continuous monitor does not retry with modification. If a protective CLOSE submission fails cleanly, the monitor's next scheduled breach evaluation either:

- Re-detects the breach → fresh `trigger_id` → new command (structurally a different command, different ID), or
- Finds the breach resolved (position moved back inside limit, or the pipeline's own close executed) → no action.

This keeps the monitor mechanical. Retry-with-modification is reserved for the PM, which can apply judgment.

---

## Uniqueness guarantees

### Within an invocation

Every PM-originated command ID within a single invocation is unique by construction, because `(envelope_id, command_ordinal, attempt_seq)` is a deterministic function of envelope structure and modification history, and the envelope ID is bijective with the source proposal.

### Across invocations

PM-originated command IDs include `invocation_id`, which is monotonically increasing and unique per pipeline invocation. A command ID from a past invocation cannot structurally appear in a new invocation.

### Monitor restarts

Engine-originated command IDs include `monitor_session_id`, which is unique per monitor process lifetime. Even if the monitor restarts and re-detects the same breach, the resulting command carries a distinct session ID.

### What happens if a duplicate command ID arrives

Abort the invocation (for PM-originated) or fail the monitor action (for engine-originated), log a structural error, raise an alert.

A duplicate arrival is a bug — either envelope state mutated concurrently, ID derivation has a flaw, the monitor reused a session ID, or something upstream is corrupt. None of these are safe to paper over by returning a stored response or silently accepting the duplicate. This is the fail-closed discipline applied at the command-intake seam.

---

## Invocation-level idempotency

Scenario: a single pipeline invocation runs twice (scheduler bug, operator re-run, process supervisor confusion). Already addressed by existing infrastructure; no new mechanism is needed.

- **APScheduler `max_instances=1`** ([infrastructure.md](../architecture/infrastructure.md)) prevents concurrent in-process invocations. A second trigger firing while the first is running is suppressed at the scheduler layer.
- **Monotonically increasing `invocation_id`** ([state-persistence.md §Invocation records](05-execution-layer/state-persistence.md)) means a restart or re-run generates a fresh ID; collision isn't possible by construction.
- **Uniqueness constraint on the invocation record** in the persistence layer catches any accidental reuse at the write layer — the second attempt errors out before any agents run.

If any of these three safeguards fails, it is a bug in the named infrastructure component, not a case for command-level compensation.

---

## Why not dedup

Conventional distributed-systems idempotency stores a command ID → response mapping and returns the stored response on duplicate arrivals. AlphaMind does not need this because the architecture structurally rules out the scenarios that would produce duplicates:

- **Pipeline process and OMS are in-process** ([infrastructure.md](../architecture/infrastructure.md), [component-boundaries.md](../architecture/component-boundaries.md)). PM→OMS is a function call, not a network RPC. Either the call returns or it raises — no "submitted but response unknown" state.
- **State mutations are atomic transactions** ([state-persistence.md §Phase 2 write path](05-execution-layer/state-persistence.md) — *"Each command's state mutations are committed atomically. If a command fails mid-processing, the transaction rolls back"*). No partial commits.
- **Fail-closed policy** ([llm-agent-failure-handling.md](llm-agent-failure-handling.md), [mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md)): if the OMS raises, the pipeline aborts the invocation. No retry loop.
- **Pipeline aborts do not resume** ([mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md)). A mid-invocation crash discards in-flight state; the next scheduled invocation starts fresh with different `invocation_id` and fresh agent output.

Stepping through the scenarios that would motivate dedup in a different system:

| Scenario | How the architecture handles it |
|---|---|
| PM emits envelope, OMS processes, returns success | Happy path. No duplicate. |
| PM emits envelope, OMS rejects synchronously | PM modifies → new envelope, new `attempt_seq`, new `command_id`. Not a duplicate. |
| OMS raises mid-command (transaction rolls back) | Pipeline aborts per fail-closed. No retry within the invocation. |
| Pipeline crashes mid-submission | Invocation aborts per mid-pipeline-failure. No resume, no retry at the next invocation with the same ID. |
| Scheduler re-runs the same invocation | Prevented by `max_instances=1`, monotonic `invocation_id`, uniqueness constraint. |
| Monitor restart re-detects breach | New `monitor_session_id` → new command ID. Re-detection is correct behavior, not a duplicate. |

None of these scenarios produce a same-ID resubmission with the same content. The only remaining way a duplicate could arrive is a behavioral or implementation bug, which fail-closed treats as an error (see [§What happens if a duplicate command ID arrives](#what-happens-if-a-duplicate-command-id-arrives)).

---

*Cross-references:*

- Envelope specification (prose): [04-decision-layer/portfolio-manager.md](04-decision-layer/portfolio-manager.md)
- PM envelope schema (JSON Schema): [04-decision-layer/pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md)
- Engine envelope schema (JSON Schema): [05-execution-layer/engine-envelope-schema.md](05-execution-layer/engine-envelope-schema.md)
- OMS command vocabulary (prose): [05-execution-layer/oms-commands.md](05-execution-layer/oms-commands.md)
- OMS command schema (JSON Schema): [05-execution-layer/oms-command-schema.md](05-execution-layer/oms-command-schema.md)
- Analyst output schema (source of `REC-<n>`): [04-decision-layer/analyst-output-schema.md](04-decision-layer/analyst-output-schema.md)
- Strategist output schema (source of `SA-<n>`, `SA-ORD-<n>`): [04-decision-layer/strategist-output-schema.md](04-decision-layer/strategist-output-schema.md)
- Activity log and atomic transactions: [05-execution-layer/state-persistence.md](05-execution-layer/state-persistence.md)
- Continuous monitor and engine-originated envelopes: [05-execution-layer/architecture.md](05-execution-layer/architecture.md)
- Broker submission retry policy (separate seam): [05-execution-layer/state-persistence.md § Phase 2 write path](05-execution-layer/state-persistence.md), [05-execution-layer/broker-adapter.md](05-execution-layer/broker-adapter.md)
- In-process architecture: [../architecture/component-boundaries.md](../architecture/component-boundaries.md), [../architecture/infrastructure.md](../architecture/infrastructure.md)
