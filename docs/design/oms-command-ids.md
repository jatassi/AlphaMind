# OMS command IDs

Deterministic identifiers for every OMS command. Specifies generation, format, uniqueness, and error handling for duplicates. Purpose is traceability and audit-log correlation; the architecture rules out the scenarios that would require network-style dedup ([§Why not dedup](#why-not-dedup)).

---

## Scope

Command ID generation for the two origins defined in [oms-commands.md §Command origins](05-execution-layer/oms-commands.md):

- **PM-originated** — extracted from envelopes produced by the PM during Phase 2 of an invocation.
- **Engine-originated** — CLOSE commands issued by the continuous monitor between invocations via engine-originated envelopes.

Out of scope: broker submission retry semantics and surfacing of failed submissions — see [state-persistence.md § Phase 2 write path](05-execution-layer/state-persistence.md).

---

## PM-originated command IDs

Derived by the OMS command intake layer at envelope receipt time from the envelope's structural position. The PM never generates, reads, or reasons about command IDs — it produces envelopes; infrastructure assigns identifiers. Parallels the principle in [oms-commands.md](05-execution-layer/oms-commands.md) that keeps LLMs from providing hallucinable values.

### Format

```
{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}
```

- **`invocation_id`** — assigned by the pipeline at invocation start, monotonically increasing ([state-persistence.md §Invocation records](05-execution-layer/state-persistence.md)).
- **`envelope_id`** — deterministically derived from the source proposal: `ENV-<source_id>`. Pattern: `^ENV-(REC|SA|SA-ORD)-[0-9]+$`.
  - `ENV-REC-<n>` for analyst recommendations (source `^REC-[0-9]+$`, see [analyst-output-schema.md](04-decision-layer/analyst-output-schema.md)).
  - `ENV-SA-<n>` for strategist per-position assessments (source `^SA-[0-9]+$`, see [strategist-output-schema.md](04-decision-layer/strategist-output-schema.md)).
  - `ENV-SA-ORD-<n>` for strategist pending-order assessments (source `^SA-ORD-[0-9]+$`).
  - The three prefixes are disjoint; envelope ID is bijective with its source.
- **`command_ordinal`** — 0-indexed position within the envelope's commands array. Steady-state envelopes produce at most one command, so this is almost always `0`; the field preserves optionality.
- **`attempt_seq`** — computed by the OMS at receipt as the count of post-rejection modifications in the envelope's modifications array ([§attempt_seq computation](#attempt_seq-computation)). First submission is `0`; each guardrail-rejection-driven modification increments.

### `attempt_seq` computation

The envelope's `modifications` array contains entries from two phases:

- **Pre-submission** (e.g., `conviction_disagreement`, `sizing_adjustment`) — applied by the PM at authoring time, baked in before first submission. Do not affect `attempt_seq`.
- **Post-rejection** (`guardrail_rejection_response`) — appended after a synchronous guardrail rejection. Each entry increments `attempt_seq`.

Computation: `attempt_seq = count(modifications where phase == "post_rejection")`. The envelope carries its own attempt history; no side state.

The schema partitions phases via an explicit `phase: "pre_submission" | "post_rejection"` enum on each entry. Formalized in [pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md): `guardrail_rejection_response` entries are always `post_rejection`; all other categories are always `pre_submission`.

### Retry and modification model

Each envelope retry is a *new* command with a new ID:

- **Modified retry after guardrail rejection.** PM appends a `guardrail_rejection_response` entry, revises the embedded command (smaller size, different strike), pipeline resubmits. OMS sees one more `post_rejection` entry → `attempt_seq` increments → new `command_id`. New command, validated against fresh state.
- **Same-content retry.** The architecture (in-process pipeline and OMS, atomic transactions, fail-closed mid-pipeline policy) produces no same-ID resubmission pathway — see [§Why not dedup](#why-not-dedup).

### Worked example

Invocation `inv-2026-04-23T14-30Z`. Analyst produces `REC-2`; PM emits envelope `ENV-REC-2`, pre-submission `conviction_disagreement` reduces size from 4% to 3%.

1. Pipeline hands `ENV-REC-2` to OMS. Post-rejection count: 0 → `attempt_seq = 0`. ID: `inv-2026-04-23T14-30Z.ENV-REC-2.0.0`.
2. Guardrail rejects (sector concentration shifted during PM evaluation). OMS writes `guardrail_rejection` activity log entry, returns rejection payload synchronously.
3. PM appends `{phase: "post_rejection", category: "guardrail_rejection_response", ...}` to `ENV-REC-2.modifications`, revises the OPEN to 1.5%.
4. Pipeline resubmits. Post-rejection count: 1 → `attempt_seq = 1`. ID: `inv-2026-04-23T14-30Z.ENV-REC-2.0.1`. Different command, different ID, fresh validation.

If step 4 succeeds, activity log reflects both attempts. If rejected, `attempt_seq` goes to 2. Each attempt is a first-class command for audit and feedback-loop purposes.

---

## Engine-originated command IDs

Parallel scheme, scoped to continuous monitor sessions rather than invocations.

### Format

```
MON.{monitor_session_id}.{trigger_id}.{command_ordinal}
```

- **`MON` prefix** — disambiguates from PM-originated (which begin with `inv-…`).
- **`monitor_session_id`** — unique per monitor process lifetime. Generated at session start (UUID or start-timestamp). Monitor restart creates a new session, so a re-detected breach after restart produces a new ID.
- **`trigger_id`** — monotonically increasing within a monitor session. Assigned when the monitor records a breach trigger ([architecture.md §4b](05-execution-layer/architecture.md)).
- **`command_ordinal`** — 0-indexed within the trigger. Always `0` in practice: per [architecture.md §4b](05-execution-layer/architecture.md), a trigger produces a single CLOSE.

### No `attempt_seq`

The monitor does not retry with modification. If a protective CLOSE submission fails cleanly, the next scheduled breach evaluation either:

- Re-detects the breach → fresh `trigger_id` → new command, or
- Finds the breach resolved (position moved back inside limit, or pipeline's own close executed) → no action.

Retry-with-modification is reserved for the PM.

---

## Uniqueness guarantees

### Within an invocation

Every PM-originated ID is unique by construction: `(envelope_id, command_ordinal, attempt_seq)` is a deterministic function of envelope structure and modification history, and envelope ID is bijective with source proposal.

### Across invocations

PM-originated IDs include `invocation_id`, monotonically increasing and unique per invocation. Past-invocation IDs cannot structurally appear in a new invocation.

### Monitor restarts

Engine-originated IDs include `monitor_session_id`, unique per monitor process lifetime. A re-detected breach after restart produces a distinct session ID.

### What happens if a duplicate command ID arrives

Abort the invocation (PM-originated) or fail the monitor action (engine-originated), log a structural error, raise an alert.

A duplicate is a bug — concurrent envelope state mutation, flawed ID derivation, reused session ID, or upstream corruption. Fail-closed discipline applied at the command-intake seam.

---

## Invocation-level idempotency

Scenario: an invocation runs twice (scheduler bug, operator re-run, supervisor confusion). Addressed by existing infrastructure:

- **APScheduler `max_instances=1`** ([infrastructure.md](../architecture/infrastructure.md)) suppresses concurrent invocations at the scheduler layer.
- **Monotonically increasing `invocation_id`** ([state-persistence.md §Invocation records](05-execution-layer/state-persistence.md)) — restart or re-run generates a fresh ID; collision impossible by construction.
- **Uniqueness constraint on the invocation record** catches any accidental reuse at the write layer — the second attempt errors before any agents run.

If any of these fail, it's a bug in the named component, not a case for command-level compensation.

---

## Why not dedup

Conventional distributed-systems idempotency stores a command ID → response mapping and returns the stored response on duplicates. AlphaMind doesn't need this because the architecture rules out duplicate scenarios:

- **Pipeline and OMS are in-process** ([infrastructure.md](../architecture/infrastructure.md), [component-boundaries.md](../architecture/component-boundaries.md)). PM→OMS is a function call, not a network RPC. Either the call returns or raises — no "submitted but response unknown" state.
- **State mutations are atomic transactions** ([state-persistence.md §Phase 2 write path](05-execution-layer/state-persistence.md) — *"Each command's state mutations are committed atomically. If a command fails mid-processing, the transaction rolls back"*).
- **Fail-closed policy** ([llm-agent-failure-handling.md](llm-agent-failure-handling.md), [mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md)): OMS raises → pipeline aborts. No retry loop.
- **No resume after abort** ([mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md)). Mid-invocation crash discards in-flight state; next invocation starts fresh with a different `invocation_id`.

Scenarios that motivate dedup elsewhere:

| Scenario | How the architecture handles it |
|---|---|
| PM emits envelope, OMS processes, returns success | Happy path. No duplicate. |
| PM emits envelope, OMS rejects synchronously | PM modifies → new envelope, new `attempt_seq`, new `command_id`. Not a duplicate. |
| OMS raises mid-command (transaction rolls back) | Pipeline aborts per fail-closed. No retry within invocation. |
| Pipeline crashes mid-submission | Invocation aborts. No resume, no same-ID retry next invocation. |
| Scheduler re-runs same invocation | Prevented by `max_instances=1`, monotonic `invocation_id`, uniqueness constraint. |
| Monitor restart re-detects breach | New `monitor_session_id` → new command ID. Correct behavior, not a duplicate. |

A duplicate arrival is a behavioral or implementation bug, treated as an error (see [§What happens if a duplicate command ID arrives](#what-happens-if-a-duplicate-command-id-arrives)).

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
