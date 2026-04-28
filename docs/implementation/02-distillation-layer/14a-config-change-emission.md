---
status: blocked
completed_date:
commit_id:
---

# 14a — Config-change activity-log emission (SQL persistence)

## Blocker

This story is blocked on the execution-layer state-persistence substrate. The atomic-emission contract from [`state-persistence.md`](../../../docs/design/05-execution-layer/state-persistence.md) requires both an `invocations` row and an `activity_log` row written inside a single transaction, and the prior-reload's `new_hash` queried from a persisted `activity_log` table — none of which exist today. Story 15 (emergency-invocation review report) is blocked on the same substrate.

When the substrate lands, set `status: not_started` and remove this section. The substrate this story needs:

- A SQL `invocations` table per [`state-persistence.md § Tier 2 — Lifecycle entities`](../../../docs/design/05-execution-layer/state-persistence.md), with at minimum the `invocation_id`, `start_timestamp`, `git_sha_at_invocation`, `trigger_type`, `trigger_reason`, and `resolved_config_hash` fields. (The full invocation-record schema is broader; this story only consumes a subset.)
- A SQL `activity_log` table per [`state-persistence.md § Activity log entries`](../../../docs/design/05-execution-layer/state-persistence.md), capable of storing the full `ActivityLogEntry` shape (entry_id, invocation_id, timestamp, event_type, event_group, position_id, order_id, thesis_id, source, detail JSON). Foreign-key from `activity_log.invocation_id` to `invocations.invocation_id`.
- A read API on the persistence layer for "the most recent `distillation_config_change` activity-log entry's `new_hash`" — needed to detect identical reloads and suppress emission.
- A transaction-context entry point the configuration loader can opt into so the invocation row's INSERT and the activity-log entry's INSERT (when emitted) commit as a unit, satisfying the rollback-atomicity contract from [`state-persistence.md § Phase 1 write path`](../../../docs/design/05-execution-layer/state-persistence.md).

The substrate is the proper subject of an execution-layer state-persistence implementation track; building it ahead of that track risks producing a Tier-2-orphan with no Tier 1 (`positions`, `theses`, `orders`, `brackets`, `cash_ledger`, `fill_records`) under it, which is the wrong build order per [`state-persistence.md § Logical entities`](../../../docs/design/05-execution-layer/state-persistence.md).

## Goal

Wire `build_distillation_config_change_entry` (from sibling story `14a-config-change-contract`) into the configuration loader so a `distillation_config_change` activity-log entry persists to the SQL `activity_log` table inside the same transaction as the invocation row, every time `config/distillation.yaml` reloads with content that differs from the prior reload. Operator-side workflow (calibration-log commit-message convention) and contract surface (event type, detail class, diff helper) are already in place from the contract sub-story.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — "No silent threshold mutation" and the "Calibration log convention" subsection
- `docs/design/05-execution-layer/state-persistence.md` § Activity log entries, § Configuration events, § Phase 1 write path — the catalog entry, the emission rule, and the atomic-transaction guarantee this story integrates with
- `docs/design/configuration-management.md` § Reload model — when reload happens (start of every invocation) and the fail-closed contract on reload failure
- `docs/implementation/02-distillation-layer/14a-config-change-contract.md` — the contract sibling whose pure-function helpers this story consumes
- `src/alphamind/config/load.py` — `load_full_config` (foundation/configuration story 08) and `src/alphamind/data_sources/_common.py:load_config()` (older aggregate that loads `DistillationConfig`); this story picks one as the integration point and surfaces the choice. See Notes.
- The execution-layer state-persistence implementation track once it lands — the canonical `invocations` and `activity_log` SQLAlchemy models, Alembic migration, and session/transaction wiring.

## Depends on

- `14a-config-change-contract` — the pure-function diff helper, the `DISTILLATION_CONFIG_CHANGE` event type, and the `CONFIG_RELOAD` source value all come from there.
- The execution-layer state-persistence track — `invocations` and `activity_log` SQL tables plus the transactional invocation-context object.

## Scope

In scope (specified at the contract level; concrete signatures depend on what the substrate track ships):

- A SQLAlchemy session-aware emission function:
  - Reads the most recent persisted `distillation_config_change` activity-log entry's `new_hash` (or `None` if none exists).
  - Loads the new `DistillationConfig` and computes its hash via `compute_distillation_config_hash`.
  - Calls `build_distillation_config_change_entry` with the prior-hash-derived `prior` reconstruction (see Notes for the comparison strategy) or directly with the prior hash if the substrate exposes one.
  - When the helper returns a non-`None` entry, persists it to the `activity_log` table inside the same SQLAlchemy session that records the invocation row.
  - When the helper returns `None`, persists no entry.

- Loader integration: the chosen loader entry point (`load_full_config` or the data-layer `load_config`) accepts a SQLAlchemy session (or a transactional context object provided by the substrate track) and invokes the emission function as the final step before the loader returns. Failure to emit aborts the invocation per [`configuration-management.md § Reload model`](../../../docs/design/configuration-management.md#reload-model).

- Hash-only comparison strategy: per the contract sibling's `build_distillation_config_change_entry`, computing the per-key change array requires both the prior and new `DistillationConfig` instances, not just hashes. The substrate work or this story owns the storage of the prior resolved-config (e.g., re-parsing the snapshot JSON written by `persist_snapshot` keyed on the prior invocation's `resolved_config_snapshot_reference`). See Notes for the resolved approach once the substrate ships.

- Tests:
  - First-ever reload (no prior `distillation_config_change` entry) writes one entry with `prior_hash=None` and an empty `changes` array.
  - Identical reload (same YAML content as the prior persisted entry) writes no new entry.
  - YAML diff with one changed key writes a single entry with the correct `prior_hash`, `new_hash`, and a single-element `changes` array.
  - YAML diff with multiple changed keys writes a single entry with all changes in the `changes` array (sorted ascending).
  - YAML diff with a nested change (e.g., a value inside `regime_classification`) produces a `changes` entry with the dotted `key_path`.
  - The entry's `source` is `EventSource.CONFIG_RELOAD`.
  - The entry's `git_sha` matches the value the invocation row records (cross-reference of the same git SHA).
  - **Atomic-emission test**: a transaction rollback on the invocation row also rolls back the activity-log entry. The test simulates a post-emission failure inside the transaction and asserts that querying after the rollback finds neither the invocation row nor the activity-log entry.

Out of scope:

- Diff detection for other config files (`assets.yaml`, `agents.yaml`, etc.). The event type is `distillation_config_change`; broader scope is a future story.
- Operator-side commit-message linting or git-hook enforcement of the calibration-log trailer convention.
- Surfacing the event type in the command-center activity-log explorer (the explorer renders any registered event type generically).
- Notifying or alerting on threshold edits (the activity-log entry IS the notification surface; Discord wiring is operator-driven via the alert registry).
- Class B state changes (refresh-primitive territory, story 07).

## Notes

The integration site for the emission function depends on which loader becomes "the" pipeline configuration loader once the execution-layer state-persistence track ships:

- `src/alphamind/config/load.py:load_full_config()` — the foundation/configuration story-08 entry point, currently does not load `DistillationConfig`.
- `src/alphamind/data_sources/_common.py:load_config()` — the older aggregate that already loads `DistillationConfig` per story 02.

A precondition of this story is convergence on a single loader (or an explicit decision that the two coexist with one of them owning distillation-config emission). Surface the choice; do not silently pick.

The "compare hashes only, suppress emission when equal" rule lets this story avoid persisting the full prior `DistillationConfig` if the substrate exposes a "fetch latest `new_hash`" read. When the hashes differ, the change-array computation requires both instances. The simplest substrate-friendly approach: re-parse the prior invocation's persisted `data/provenance/invocations/{prior_invocation_id}/resolved_config.json` (already produced by `persist_snapshot`) to reconstruct the prior `DistillationConfig`, then call `compute_distillation_config_diff(prior=reconstructed, new=current)`. The contract sibling's diff helper is structured to make this straightforward — it accepts `DistillationConfig | None`.

Per CLAUDE.md and the user's "harness is calibrated, not pessimistic" memory, the failure-mode posture here is fail-closed: any error during emission (substrate unavailable, prior snapshot missing, hash computation failure) aborts the invocation rather than silently writing a degraded entry.

The atomic-emission test is the load-bearing test for this story. Without it, the story's only structural difference from the contract sibling is "the entry persists somewhere." Testing rollback atomicity proves that the per-invocation emission cannot leave a half-state where the activity-log entry exists without the invocation row, which is the failure mode the spec calls out.

## Acceptance criteria

- [ ] The configuration loader emits exactly one `distillation_config_change` entry per invocation when the resolved `DistillationConfig` hash differs from the prior reload.
- [ ] The configuration loader emits no entry when the hash matches the prior reload.
- [ ] On the first-ever reload, an entry is emitted with `prior_hash=None` and an empty `changes` array.
- [ ] The entry's `source` field is `EventSource.CONFIG_RELOAD`.
- [ ] The entry's `git_sha` matches the corresponding invocation row's recorded git SHA.
- [ ] The `changes` array is sorted by `key_path` ascending and uses dotted notation matching the `DistillationConfig` field structure.
- [ ] A transaction rollback on the invocation row also rolls back the activity-log entry (atomic-emission test).
- [ ] Emission failure aborts the invocation per [`configuration-management.md § Reload model`](../../../docs/design/configuration-management.md#reload-model).
- [ ] Tests cover: first-reload baseline, no-change suppression, single-key change, multi-key change, nested-key change, rollback atomicity, `source` value, `git_sha` correspondence.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
