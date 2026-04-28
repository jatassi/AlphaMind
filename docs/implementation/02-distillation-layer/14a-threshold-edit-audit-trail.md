---
status: in_progress
completed_date:
commit_id:
---

# 14a — Threshold-edit activity-log entry and config-reload diff emission

## Goal

Implement the audit trail that enforces "no silent threshold mutation" from the threshold-calibration design doc. Add a `distillation_config_change` event type to the activity log catalog, and emit one entry every time `config/distillation.yaml` reloads with values that differ from the prior reload. Pair the activity log entry with a documented commit-message convention so the operator's calibration log (motivating observation, old value, new value) lives in version control alongside the YAML diff.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — "No silent threshold mutation" paragraph naming the activity-log entry and the calibration-log-in-version-control convention; review-procedure step 4 names the replay harness as the regime-sensitive evidence step whose `report_id` the calibration log cites
- `docs/design/02-distillation-layer/threshold-calibration.md` § Static configuration thresholds — the universe of keys this story diffs against; the section-prefix split (`anomaly_detection.*`, `regime_classification.*`, `persistence_windows.*`) defines what counts as a "regime-sensitive edit" for the trailer convention below
- `docs/design/02-distillation-layer/replay-harness.md` — produces the per-regime flag-rate report at `data/replay_reports/{report_id}/report.md`; the `report_id` is what the calibration-log trailer carries
- `docs/design/05-execution-layer/state-persistence.md` § Activity log entries — entry shape and event-type catalog (the new event type extends this catalog)
- `docs/design/05-execution-layer/state-persistence.md` § Event type catalog — convention for declaring new event types and their detail payloads
- `docs/design/configuration-management.md` § Reload model — when reload happens (start of every invocation) and what failure-mode aborts the invocation
- `docs/design/command-center.md` § Activity log explorer — the operator-facing surface that will display the new event type
- `src/alphamind/config/_common.py` (or equivalent) — where `load_config()` lives; the reload-time diff emission integrates here
- `src/alphamind/persistence/models.py` — where the activity-log SQLAlchemy model lives
- Story `02-config-schema.md` — establishes `DistillationConfig` and the load path; this story consumes both

## Depends on

- 02 (Distillation config schema) — the typed `DistillationConfig` object is what gets diffed; the story reuses its field structure to flatten old vs. new into a per-key change list.

## Scope

In scope:

- **Catalog extension**. Add a `distillation_config_change` event type to `docs/design/05-execution-layer/state-persistence.md § Event type catalog` under a new "Configuration events" subsection. Detail payload schema:
  - `config_file`: string — always `"config/distillation.yaml"` for this story; the field exists so a future story can extend the same event type for other config files.
  - `prior_hash`: string — SHA-256 of the resolved config object before this reload (or `null` on first reload).
  - `new_hash`: string — SHA-256 of the resolved config object after this reload.
  - `changes`: array of objects, each with `key_path` (dotted path into the YAML, e.g., `anomaly_detection.volume_anomaly_sigma`), `old_value`, `new_value`. Empty array means the file reloaded cleanly with no diff (in which case the entry is NOT emitted; see emission rule below).
  - `git_sha`: string — the HEAD commit SHA at reload time, so the activity log links forward into the operator's calibration-log commit message.

- **Emission rule**. At the start of each invocation, when `load_config()` runs:
  1. Compute the SHA-256 of the resolved `DistillationConfig` object (deterministic key order; reuse the framework's existing config-hash helper if one exists, else add one keyed on `model_dump(mode="json", sort_keys=True)`).
  2. Read the most recent prior `distillation_config_change` activity log entry's `new_hash` (or treat as `null` if none exists).
  3. If the hashes differ, compute the per-key change list by recursive diff of the two `DistillationConfig` instances, and emit a single `distillation_config_change` entry with the populated `changes` array.
  4. If the hashes match, emit nothing.
  5. The emission runs inside the same transaction that records the invocation row, so a partially-emitted entry never persists. Failure to emit aborts the invocation per the existing fail-closed reload contract.

- **Source attribution**. The entry's `source` is `config_reload` (a new value in the existing activity-log `source` enum, distinct from `pipeline`, `monitor`, `operator_console`). Add the value to whatever code-side enum or CHECK constraint enumerates `source`.

- **Commit-message convention**. Add a "Calibration log convention" subsection to `docs/design/02-distillation-layer/threshold-calibration.md § Update process` (after the "No silent threshold mutation" paragraph) documenting the recommended commit-message structure for threshold-edit commits. Use a structured trailer block:

  ```
  calib: <YAML key path> <old> -> <new>

  Motivating observation: <one-paragraph why>

  Empirical inputs: <flag-rate report or feedback-loop output the operator consulted>

  Replay-harness report: <report_id>          (required for regime-sensitive edits)
  ```

  Multiple `calib:` trailers per commit are allowed. The git history paired with the activity-log entries forms the full audit trail: activity log = "what changed and when," commit message = "why."

  **`Replay-harness report:` semantics.** Required when the edited key path falls under `anomaly_detection.*`, `regime_classification.*`, `regime_transition.*`, `lead_lag.*`, `narrative_lag.*`, or `persistence_windows.*` — the regime-sensitive set named by [`threshold-calibration.md § Update process` step 4](../../../design/02-distillation-layer/threshold-calibration.md#update-process). Optional for `prediction_market.*` edits. The `<report_id>` is the directory name under `data/replay_reports/` produced by [`replay-harness.md`](../../../design/02-distillation-layer/replay-harness.md), and is the same identifier the operator cites in the corresponding `/feedback-validate` registration's `expected_magnitude` or `success_criterion` field — pairing the calibration-log commit and the validation registration on a single regime-grounded evidence anchor. When multiple `calib:` trailers in one commit touch the regime-sensitive set, one `Replay-harness report:` line is sufficient if the same harness run covered all the edited keys; otherwise list one per evidence run.

- **Tests**:
  - Unit test: a fresh database with no prior `distillation_config_change` entry plus a loaded `DistillationConfig` produces exactly one entry on first reload, with `prior_hash = null` and an empty `changes` array, recording the bootstrap baseline.
  - Unit test: identical reload (same YAML content) produces no new entry on the second invocation.
  - Unit test: a YAML with one changed key produces an entry with `changes = [{key_path: ..., old_value: ..., new_value: ...}]` and the correct `prior_hash` / `new_hash`.
  - Unit test: a YAML with multiple changed keys produces a single entry with all changes in the `changes` array.
  - Unit test: a YAML with a nested change (e.g., a value inside `regime_classification`) produces a `changes` entry with the dotted `key_path`.
  - Unit test: the entry's `source` is `config_reload`.
  - Unit test: the entry's `git_sha` matches the value the invocation row records (cross-reference of the same git SHA).
  - Unit test: a transaction rollback on the invocation row also rolls back the activity-log entry (atomic-emission test).

Out of scope:
- Diff detection for other config files (`assets.yaml`, `agents.yaml`, etc.). The event type is named `distillation_config_change`; a future story handles other configs.
- Operator-side commit-message linting or git-hook enforcement of the convention.
- Surfacing the event type in the command-center activity-log explorer (the explorer renders any registered event type generically; no special-case rendering needed for v1).
- Notifying or alerting on threshold edits (the activity-log entry IS the notification surface; the operator decides whether to wire it into Discord webhooks separately via the alert registry).
- Class B state changes — those are not threshold edits, they are computed rolling state and are owned by the refresh primitive in story 07.

## Notes

The "first reload emits a baseline entry" rule is the deliberate choice: every invocation has a discoverable predecessor entry, so the operator-driven monthly review can reconstruct the full timeline without external knowledge. The alternative — suppressing the first entry — keeps the log shorter but creates a "missing prior" gap that hurts review.

The `changes` array's `key_path` uses dotted notation matching the Pydantic model's field structure. For example, the volume anomaly sigma is `anomaly_detection.volume_anomaly_sigma`, not `volume_anomaly_sigma`. This makes activity-log queries filterable by group prefix (e.g., grep for `regime_classification.*` to find all regime-boundary edits over a window).

The `git_sha` field is what binds the activity-log entry to the commit message. The invocation row already records the git SHA at invocation start per `state-persistence.md § Code state fields`. The new event type carries the same SHA so the activity-log entry stands alone — an operator querying the entry doesn't need to also fetch the invocation row to know which commit's calibration-log message to read.

The recursive diff implementation should produce a stable order (sort by `key_path` ascending) so two reloads with the same diff produce byte-identical event-detail JSON. This matters for archive diffability and for any downstream LLM that consumes the entry verbatim.

The commit-message trailer convention is a recommendation, not enforcement. Per CLAUDE.md the user's bar for adding hooks/linters is high, and the user's "simplify before building" memory argues against premature mechanization. The convention lives in the design doc as guidance; the operator follows it manually. If trailer adherence becomes a problem, a future hook story can be added — but not by this story.

The regime-sensitive prefix list (`anomaly_detection.*`, `regime_classification.*`, `regime_transition.*`, `lead_lag.*`, `narrative_lag.*`, `persistence_windows.*`) covers every threshold whose flagging or labeling behavior shifts when the underlying volatility regime shifts — the confounder the replay harness exists to neutralize per [`replay-harness.md § Why the harness exists`](../../../design/02-distillation-layer/replay-harness.md#why-the-harness-exists). The `prediction_market.*` prefix is excluded because its 5pp threshold is universe-wide and not regime-conditioned (per [`threshold-calibration.md § Prediction market delta`](../../../design/02-distillation-layer/threshold-calibration.md#prediction-market-delta)). The list is documented in this story's design subsection and in the design doc itself; it is operator-side review-procedure guidance, not a runtime check.

The `source: config_reload` value is new. Verify the existing `source` enum (or CHECK constraint) is extensible — if it's a typed enum in code, add the new value; if it's a CHECK constraint in SQL, the migration extends it. Mirror however the existing `pipeline` / `monitor` / `operator_console` values are declared.

## Acceptance criteria

- [ ] `docs/design/05-execution-layer/state-persistence.md § Event type catalog` includes a "Configuration events" subsection with `distillation_config_change` documented including the detail payload schema described above.
- [ ] The new event type's detail payload schema is enforced (Pydantic model or equivalent) at emission time.
- [ ] The configuration loader emits exactly one `distillation_config_change` entry per invocation when the resolved `DistillationConfig` hash differs from the prior reload.
- [ ] The configuration loader emits no entry when the hash matches the prior reload.
- [ ] On first-ever reload, an entry is emitted with `prior_hash = null` and an empty `changes` array (recording the bootstrap baseline).
- [ ] The entry's `source` field is `config_reload`; the value is registered in whatever enum or CHECK constraint enumerates `source`.
- [ ] The entry's `git_sha` matches the corresponding invocation row's recorded git SHA.
- [ ] The `changes` array uses dotted `key_path` notation matching the Pydantic field structure (e.g., `regime_classification.regime_low_vol_vix_max`).
- [ ] The `changes` array is sorted by `key_path` ascending for deterministic JSON output.
- [ ] A transaction rollback on the invocation row also rolls back the activity-log entry (atomic-emission test).
- [ ] `docs/design/02-distillation-layer/threshold-calibration.md § Update process` includes a "Calibration log convention" subsection documenting the recommended commit-message trailer block.
- [ ] The Calibration log convention enumerates `Replay-harness report: <report_id>` as required for edits under `anomaly_detection.*`, `regime_classification.*`, `regime_transition.*`, `lead_lag.*`, `narrative_lag.*`, or `persistence_windows.*`, with a cross-reference to [`replay-harness.md`](../../../design/02-distillation-layer/replay-harness.md) for the `report_id` provenance and to [`threshold-calibration.md § Update process` step 4](../../../design/02-distillation-layer/threshold-calibration.md#update-process) for the regime-sensitive-set definition.
- [ ] Unit tests cover: first-reload baseline, no-change suppression, single-key change, multi-key change, nested-key change, rollback atomicity, `source` value, `git_sha` correspondence.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
