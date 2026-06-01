# 01a — PROFILE_SWITCHED event substrate

## Goal

Ship the activity-log substrate that the future `POST /api/control/switch_profile` proxy (story 04a) writes through when the operator changes the active profile. Per the [ALP-649](<https://linear.app/alphamind-jatassi/issue/ALP-649>) drafting note carried on [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center), the substrate is: new `EventType.PROFILE_SWITCHED` enum member, `ProfileSwitchedDetail` frozen dataclass mirroring `DistillationConfigChangeDetail`, Alembic migration regenerating the `ck_activity_log_event_type` CHECK constraint, and an `emit_profile_switch_entry` helper that appends a typed `ActivityLogEntry` inside an open `InvocationHandle`. No production caller in this story; the caller lands in story 04a.

## Reading

* `src/alphamind/portfolio_state/events/configuration.py` — `DistillationConfigChangeDetail` is the canonical sibling pattern; mirror its shape (frozen dataclass + `_REGISTRY` registration).
* `src/alphamind/portfolio_state/events/types.py` — `EventType` enum + `EventGroup.CONFIGURATION` placement.
* `src/alphamind/portfolio_state/events/codec.py` — `EVENT_TYPE_TO_DETAIL_CLASS` registry the new dataclass plugs into.
* `src/alphamind/portfolio_state/events/__init__.py` and `events/activity_log.py` — public-surface re-exports the new type must join.
* `src/alphamind/state/invocation_context/config_change.py` — `emit_distillation_config_change_entry` is the canonical sibling emitter; the new helper mirrors its open-transaction semantics.
* `src/alphamind/config/control_handlers/profile_switch.py` — `switch_active_profile()` + `ProfileSwitchOutcome`; the emitter helper accepts the outcome.
* `src/alphamind/persistence/migrations/versions/b2c4f7a3d9e8_extend_activity_log_monitor_event_types.py` and `d8a3f2c7b9e4_extend_activity_log_reconciliation_correction.py` — canonical CHECK-constraint regeneration migrations to model after.
* [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (D) — `OperatorInvocationHandle` binding shape the future caller (story 04a) uses.

## Depends on

* (none — this is a leaf substrate story; parallel with 01b and 01c)

## Scope

In scope, all under `src/alphamind/`. Tests at `tests/portfolio_state/events/`, `tests/state/invocation_context/`, and `tests/persistence/migrations/`.

### 1\. `EventType.PROFILE_SWITCHED`

Add the new enum member to `portfolio_state/events/types.py`. Place inside `EventGroup.CONFIGURATION` alongside `DISTILLATION_CONFIG_CHANGED`.

### 2\. `ProfileSwitchedDetail` frozen dataclass

Add to `portfolio_state/events/configuration.py` as a sibling of `DistillationConfigChangeDetail`. Fields per the [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) drafting note:

* `previous_profile: ProfileName` — the profile recorded as active before the switch
* `new_profile: ProfileName` — the profile recorded as active after the switch
* `is_no_op: bool` — `True` when `previous_profile == new_profile`

Use the existing `ProfileName` NewType if defined; if absent, add it to the same NewType module the existing profile-related types live in (search for `ProfileName` / similar) — do not invent a new one.

Register in `_REGISTRY` so `EVENT_TYPE_TO_DETAIL_CLASS[EventType.PROFILE_SWITCHED]` returns the class. Export from `events/__init__.py` and `events/activity_log.py` alongside `DistillationConfigChangeDetail`.

### 3\. `emit_profile_switch_entry` helper

Add to `state/invocation_context/config_change.py`. Signature:

```python
def emit_profile_switch_entry(
    *,
    handle: InvocationHandle,
    outcome: ProfileSwitchOutcome,
) -> None:
    ...
```

Semantics:

* Constructs a `ProfileSwitchedDetail` from `outcome.previous_profile`, `outcome.new_profile`, `outcome.is_no_op`.
* When `outcome.is_no_op` is `True`, returns without appending — mirrors the byte-identical-suppression pattern `DistillationConfigChangeDetail` already uses.
* When `outcome.is_no_op` is `False`, appends one typed `ActivityLogEntry` inside the open handle's transaction.

### 4\. Alembic migration

New migration file under `persistence/migrations/versions/` named per the convention (`<hash>_extend_activity_log_profile_switched.py` or similar — match neighboring filenames). `upgrade()` regenerates `ck_activity_log_event_type` to include `PROFILE_SWITCHED`; `downgrade()` reverses. Mirror `b2c4f7a3d9e8_extend_activity_log_monitor_event_types.py` for the `op.drop_constraint` → `op.create_check_constraint` shape.

### Out of scope

* The HTTP route + caller that invokes `emit_profile_switch_entry` — story 04a (`/api/control/switch_profile` proxy).
* The `OperatorInvocationHandle` primitive that the future caller wraps the emit in — story 02 (backend foundation).

## Acceptance criteria

- [ ] `EventType.PROFILE_SWITCHED` exists in `portfolio_state/events/types.py` and is placed in `EventGroup.CONFIGURATION`.
- [ ] `ProfileSwitchedDetail` exists in `portfolio_state/events/configuration.py` as a frozen dataclass with `previous_profile`, `new_profile`, `is_no_op` fields.
- [ ] `EVENT_TYPE_TO_DETAIL_CLASS[EventType.PROFILE_SWITCHED]` returns `ProfileSwitchedDetail`.
- [ ] `ProfileSwitchedDetail` is exported from `portfolio_state/events/__init__.py` and `portfolio_state/events/activity_log.py`.
- [ ] `emit_profile_switch_entry` exists in `state/invocation_context/config_change.py` with the documented signature.
- [ ] `emit_profile_switch_entry` writes exactly one `ActivityLogEntry` when `outcome.is_no_op` is `False`; zero entries when `True`.
- [ ] A `ProfileSwitchedDetail` round-trips through the codec — serialised `detail_json` of an emitted row decodes back to an equal dataclass.
- [ ] New Alembic migration file exists under `persistence/migrations/versions/` whose `upgrade()` regenerates `ck_activity_log_event_type` to include `PROFILE_SWITCHED` and whose `downgrade()` reverses.
- [ ] Migration test under `tests/persistence/migrations/` asserts both directions and rejects pre-existing `PROFILE_SWITCHED` rows on downgrade only when the schema doesn't yet permit them.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.
- [ ] No production caller of `emit_profile_switch_entry` is added in this story (the caller is story 04a).

## Verification

Scoped pytest: `uv run pytest tests/portfolio_state/events/ tests/state/invocation_context/ tests/persistence/migrations/ -n auto`. Inspect with `rg "emit_profile_switch_entry" src/alphamind/` — the only hits should be the definition itself; no production call sites in this story.