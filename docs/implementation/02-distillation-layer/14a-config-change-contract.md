---
status: not_started
completed_date:
commit_id:
---

# 14a — Config-change activity-log contract (doc + in-memory)

## Goal

Land the doc-only deliverables and in-memory record contract for the threshold-edit audit trail from [`threshold-calibration.md § Update process`](../../../docs/design/02-distillation-layer/threshold-calibration.md#update-process). After this story, the calibration-log commit-message convention is documented and operator-followable, the `distillation_config_change` event type and `config_reload` source value exist as typed records, and the per-key diff that the emission step will eventually persist is computable as a pure function — but no row is yet written to a SQL store. The follow-up sub-story `14a-config-change-emission` wires the pure-function output into the OMS activity-log table once the execution-layer state-persistence substrate ships.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — "No silent threshold mutation" paragraph and the new "Calibration log convention" subsection (the trailer block, the regime-sensitive prefix list, the `Replay-harness report:` requirement)
- `docs/design/02-distillation-layer/threshold-calibration.md` § Static configuration thresholds — the universe of keys and the section-prefix split that defines the regime-sensitive set
- `docs/design/02-distillation-layer/replay-harness.md` — produces the per-regime flag-rate report at `data/replay_reports/{report_id}/report.md` whose `report_id` the calibration-log trailer carries
- `docs/design/05-execution-layer/state-persistence.md` § Activity log entries — `Source` field's broadened vocabulary (OMS subsystems, `operator_console`, `config_reload`) and the new "Configuration events" subsection naming `distillation_config_change` and its detail payload
- `docs/design/configuration-management.md` § Reload model — when reload happens (start of every invocation) and what failure-mode aborts the invocation
- `docs/implementation/01-data-layer/portfolio-state/03e-activity-log-records.md` — the existing in-memory contract this story extends (`EventGroup`, `EventType`, `EventSource`, `ActivityLogEntry`, `EVENT_TYPE_TO_DETAIL_CLASS`, `EVENT_TYPE_TO_GROUP`)
- `src/alphamind/portfolio_state/records/activity_log.py` — record module that gains the new enum members, detail class, and mapping entries
- `src/alphamind/portfolio_state/computations/activity_log.py` — sibling pure-function module the diff helper is co-located with
- `src/alphamind/config/snapshot.py` — `serialize_resolved_config` / `compute_snapshot_hash` patterns; the diff helper reuses the same canonical-JSON discipline keyed on `DistillationConfig.model_dump(mode="json")`
- `src/alphamind/config/models/distillation.py` — `DistillationConfig` whose Pydantic field structure is what dotted `key_path` traversal walks
- Story `14a-config-change-emission.md` — the deferred sibling that consumes this story's diff output and persists the entry

## Depends on

- 02 (DistillationConfig schema) — the typed config object whose diff this story computes.
- (Cross-track) Portfolio-state story 03e — `ActivityLogEntry`, `EventType`, `EventGroup`, `EventSource`, and the two `EVENT_TYPE_TO_*` mappings live here and are extended by this story. Already done.

## Scope

In scope:

### 1. Design-doc edits

- `docs/design/05-execution-layer/state-persistence.md`:
  - Source-field description harmonized with the in-code `EventSource` enum and the broader emitter vocabulary (OMS subsystems, `operator_console`, `config_reload`).
  - New "Configuration events" subsection in the event-type catalog with `distillation_config_change` and its detail payload (`config_file`, `prior_hash`, `new_hash`, `changes`, `git_sha`).
- `docs/design/02-distillation-layer/threshold-calibration.md` § Update process:
  - New "Calibration log convention" subsection after the "No silent threshold mutation" paragraph, documenting the structured commit-message trailer block and naming the regime-sensitive prefix list (`anomaly_detection.*`, `regime_classification.*`, `regime_transition.*`, `lead_lag.*`, `narrative_lag.*`, `persistence_windows.*`) that gates the `Replay-harness report:` requirement.

Both doc edits land as part of this story's commit. (They are already inflight on the working branch from the prep work that produced this story split — confirm presence before starting and skip re-edit if already landed.)

### 2. In-memory contract additions in `src/alphamind/portfolio_state/records/activity_log.py`

- New `EventGroup.CONFIGURATION` enum member.
- New `EventType.DISTILLATION_CONFIG_CHANGE` enum member.
- Two new `EventSource` enum members:
  - `CONFIG_RELOAD` — emitted by the configuration loader when a reloaded distillation config differs from the prior reload.
  - `OPERATOR_CONSOLE` — emitted by the [command center](../../../docs/design/command-center.md) on operator-driven mutations. Currently referenced 9× in `command-center.md` but absent from the in-code enum; this story closes the gap.
- New `DistillationConfigChange` Pydantic model representing one entry in the `changes` array:
  - `key_path: str` — dotted path matching the `DistillationConfig` field structure (e.g., `anomaly_detection.volume_anomaly_sigma`); non-empty.
  - `old_value: Any` — the prior-reload value at this key path, JSON-serialisable.
  - `new_value: Any` — the new-reload value at this key path, JSON-serialisable.
  - Frozen Pydantic v2 model.
- New `DistillationConfigChangeDetail` Pydantic model (per-event-type detail-payload class, frozen Pydantic v2 BaseModel):
  - `config_file: str = "config/distillation.yaml"` — non-empty; the field exists so a future story can reuse the event type for other config files.
  - `prior_hash: str | None` — SHA-256 hex digest of the prior resolved config; `None` on the first-ever reload.
  - `new_hash: str` — SHA-256 hex digest of the current resolved config; non-empty.
  - `changes: tuple[DistillationConfigChange, ...]` — sorted by `key_path` ascending; empty tuple on the first-reload baseline entry. A `model_validator` enforces sorted-ascending order.
  - `git_sha: str` — HEAD commit SHA at reload time; non-empty.
- Update `EVENT_TYPE_TO_DETAIL_CLASS` to add `EventType.DISTILLATION_CONFIG_CHANGE: DistillationConfigChangeDetail`.
- Update `EVENT_TYPE_TO_GROUP` to add `EventType.DISTILLATION_CONFIG_CHANGE: EventGroup.CONFIGURATION`.
- Update `AnyDetailType` to include `DistillationConfigChangeDetail`.

### 3. Pure-function diff helpers in `src/alphamind/portfolio_state/computations/activity_log.py`

- `def compute_distillation_config_diff(prior: DistillationConfig | None, new: DistillationConfig) -> tuple[DistillationConfigChange, ...]`:
  - Walks `prior.model_dump(mode="json")` and `new.model_dump(mode="json")` recursively.
  - Returns one `DistillationConfigChange` per scalar leaf whose value differs between `prior` and `new`. Use `mode="json"` so enum values are emitted as their string `value` (matching how the snapshot serializer renders them — `src/alphamind/config/snapshot.py`).
  - `key_path` joins nested keys with a dot: a value at `dump["anomaly_detection"]["volume_anomaly_sigma"]` produces `key_path="anomaly_detection.volume_anomaly_sigma"`.
  - Returns the diff sorted by `key_path` ascending for byte-stable JSON output downstream.
  - When `prior is None` (first reload), returns an empty tuple — the bootstrap-baseline entry carries no per-key changes; the `prior_hash=None` field is what makes it diffable.
  - The two configs must share the same field structure (both are `DistillationConfig` instances with identical schema); the helper does not handle structural drift across schema versions — that would be a separate migration concern.

- `def compute_distillation_config_hash(config: DistillationConfig) -> str`:
  - Returns the 64-hex-char SHA-256 of the canonical-JSON serialization of `config.model_dump(mode="json")` with sorted keys. Mirrors the `compute_snapshot_hash` discipline so two identical `DistillationConfig` instances produce byte-identical hashes across runs and machines.

- `def build_distillation_config_change_entry(*, prior: DistillationConfig | None, new: DistillationConfig, invocation_id: str, timestamp: datetime, git_sha: str, entry_id: str) -> ActivityLogEntry | None`:
  - Computes the prior hash (`None` if `prior is None`) and the new hash via `compute_distillation_config_hash`.
  - Returns `None` when `prior is not None` and `prior_hash == new_hash` — the no-change suppression rule from the story spec; the caller skips emission.
  - Returns a fully-populated `ActivityLogEntry` otherwise. `event_type=EventType.DISTILLATION_CONFIG_CHANGE`, `event_group=EventGroup.CONFIGURATION`, `source=EventSource.CONFIG_RELOAD`, `position_id=None`, `order_id=None`, `thesis_id=None`. The `detail` is a `DistillationConfigChangeDetail` whose `changes` tuple comes from `compute_distillation_config_diff`.
  - The function is pure — no DB, no clock, no git lookup. The caller supplies `timestamp`, `git_sha`, `entry_id`, and `invocation_id`; the emission story will wire those at the integration site.

### 4. Tests in `tests/portfolio_state/records/test_activity_log.py` and `tests/portfolio_state/computations/test_activity_log.py`

Records-side tests:

- `DistillationConfigChangeDetail` round-trips through Pydantic for a representative payload.
- `DistillationConfigChangeDetail` rejects an unsorted `changes` tuple with a `ValidationError` whose message names the offending pair.
- `DistillationConfigChangeDetail` rejects an empty `config_file`, an empty `new_hash`, or an empty `git_sha`.
- `DistillationConfigChange` rejects an empty `key_path`.
- `EVENT_TYPE_TO_DETAIL_CLASS[EventType.DISTILLATION_CONFIG_CHANGE]` is `DistillationConfigChangeDetail`.
- `EVENT_TYPE_TO_GROUP[EventType.DISTILLATION_CONFIG_CHANGE]` is `EventGroup.CONFIGURATION`.
- `ActivityLogEntry` accepts `event_type=DISTILLATION_CONFIG_CHANGE`, `event_group=CONFIGURATION`, `source=CONFIG_RELOAD`, and a `DistillationConfigChangeDetail` payload.
- `ActivityLogEntry` rejects mismatch (e.g., `DISTILLATION_CONFIG_CHANGE` paired with `EventGroup.PM_DECISION`) with the existing mismatch error.

Computations-side tests:

- `compute_distillation_config_hash` returns a 64-hex-char string, byte-identical for byte-identical inputs across two calls.
- `compute_distillation_config_diff` for two byte-identical `DistillationConfig` instances returns `()`.
- `compute_distillation_config_diff` with one differing scalar produces exactly one `DistillationConfigChange` with the correct dotted `key_path`, `old_value`, `new_value`.
- `compute_distillation_config_diff` with multiple differing scalars produces all of them sorted by `key_path` ascending.
- `compute_distillation_config_diff` with a nested change (a value inside `regime_classification`) produces a dotted `key_path` like `regime_classification.regime_low_vol_vix_max`.
- `compute_distillation_config_diff` with `prior=None` returns `()`.
- `build_distillation_config_change_entry` with `prior=None` returns an `ActivityLogEntry` whose detail has `prior_hash=None`, an empty `changes` tuple, and the correct `new_hash`.
- `build_distillation_config_change_entry` with `prior` and `new` both equal returns `None` (no-change suppression).
- `build_distillation_config_change_entry` with `prior` and `new` differing returns an `ActivityLogEntry` whose detail's `prior_hash`, `new_hash`, `changes`, and `git_sha` match the inputs and computed values.
- `build_distillation_config_change_entry`'s output is independent of wall-clock — the `timestamp` field equals the supplied `timestamp` argument.

Out of scope:

- SQL persistence of the activity-log entry (deferred to `14a-config-change-emission`).
- Wiring `build_distillation_config_change_entry` into `load_full_config` or `data_sources/_common.py:load_config()` (deferred to `14a-config-change-emission`).
- The "atomic emission" transaction-rollback test — that test requires a live DB session wrapping both the invocation row and the activity-log entry; both are absent today (deferred to `14a-config-change-emission`).
- Querying the prior `new_hash` from a persisted activity-log table (also deferred — there is no table to query yet).
- Diff detection for other config files (`assets.yaml`, `agents.yaml`, etc.); this story's event type is named `distillation_config_change` precisely to leave the broader scope to a future story.
- Operator-side commit-message linting or git-hook enforcement of the calibration-log trailer convention. Per the user's "simplify before building" memory and the original 14a Notes, mechanization waits until adherence becomes a problem.
- Surfacing the event type in the command-center activity-log explorer — the explorer renders any registered event type generically.

## Notes

The split into "contract" (this story) and "emission" (the sibling story) follows the precedent set by [`portfolio-state/04b-repository-protocol.md`](../01-data-layer/portfolio-state/04b-repository-protocol.md), which landed the read-side Protocol with a stub and explicitly deferred production wiring to the not-yet-built execution-layer state-persistence track. The same posture fits here: this story lands every contract surface — design doc, enum members, detail class, pure-function helpers, mapping entries — without committing to SQL substrate the codebase doesn't yet have.

The `EventSource.OPERATOR_CONSOLE` addition closes a pre-existing inconsistency between `state-persistence.md` (which originally listed only OMS subsystems) and `command-center.md` (which references `source: operator_console` in 9 places). Adding the value here pre-empts a duplicate fix during the command-center implementation track.

The diff helper operates on `DistillationConfig`, not `ResolvedConfig`. Distillation thresholds are not subject to the regime / overlay / mode composition cascade that produces `ResolvedConfig.rule_values` — they live as a flat Pydantic tree under `config/distillation.yaml`. The `key_path` dotted notation matches the `DistillationConfig` field structure exactly, which is what the Calibration-log trailer convention's prefix-based regime-sensitive set queries against.

The "first reload emits a baseline entry" behaviour ships with this story's `build_distillation_config_change_entry` (returns a non-`None` entry with `prior_hash=None` and empty `changes`). The downstream emission story is responsible for detecting whether a prior entry exists and supplying `prior=None` accordingly.

Per `feedback_per_producer_schema.md`, the new detail class is its own typed Pydantic model rather than a variant on an existing class. Per `feedback_no_inventing_component_names.md`, every name introduced here (`CONFIGURATION`, `DISTILLATION_CONFIG_CHANGE`, `CONFIG_RELOAD`, `OPERATOR_CONSOLE`, `DistillationConfigChangeDetail`, `DistillationConfigChange`) lands first in a design-doc anchor (the `state-persistence.md` catalog edit and the `threshold-calibration.md` calibration-log subsection edit) before appearing in code.

The `Any` typing on `old_value` / `new_value` is intentional: distillation-config values span scalars (float, int, str, bool, enum string), and the diff helper preserves whatever the Pydantic JSON dump produces. Downstream consumers (the eventual emission writer, the command-center activity-log explorer) treat the values as opaque JSON content. Tightening to a discriminated union would not add value at the contract level.

## Acceptance criteria

- [ ] `docs/design/05-execution-layer/state-persistence.md` § Activity log entries describes Source as "which subsystem generated this entry" and names OMS subsystems, `operator_console`, and `config_reload` as the three vocabularies.
- [ ] `docs/design/05-execution-layer/state-persistence.md` § Event type catalog includes a "Configuration events" subsection with `distillation_config_change` documented including `config_file`, `prior_hash`, `new_hash`, `changes`, and `git_sha` fields and the no-change suppression rule.
- [ ] `docs/design/02-distillation-layer/threshold-calibration.md` § Update process includes a "Calibration log convention" subsection with the structured trailer block and the regime-sensitive prefix list.
- [ ] `EventGroup.CONFIGURATION`, `EventType.DISTILLATION_CONFIG_CHANGE`, `EventSource.CONFIG_RELOAD`, and `EventSource.OPERATOR_CONSOLE` exist in `src/alphamind/portfolio_state/records/activity_log.py` as `StrEnum` members.
- [ ] `DistillationConfigChange` and `DistillationConfigChangeDetail` exist as frozen Pydantic v2 models with the documented fields and validators.
- [ ] `EVENT_TYPE_TO_DETAIL_CLASS` and `EVENT_TYPE_TO_GROUP` cover `EventType.DISTILLATION_CONFIG_CHANGE` and `mypy --strict` confirms no missing keys.
- [ ] `AnyDetailType` includes `DistillationConfigChangeDetail`.
- [ ] `compute_distillation_config_hash`, `compute_distillation_config_diff`, and `build_distillation_config_change_entry` exist in `src/alphamind/portfolio_state/computations/activity_log.py` with the documented signatures.
- [ ] Records-side tests cover the round-trip, the validators (sorted-ascending changes, non-empty fields), and the mapping registrations.
- [ ] Computations-side tests cover the hash determinism, the diff helper across no-change / single-key / multi-key / nested-key / `prior=None` cases, the `build_distillation_config_change_entry` no-change suppression rule, and the first-reload-baseline output.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
