---
status: not_started
completed_date:
commit_id:
---

# 03c — `scheduler.yaml` + Pydantic model

## Goal

Land `config/scheduler.yaml` (the APScheduler cron expression registry that drives every pipeline invocation) and a Pydantic model that type-checks it. This file is the canonical source for the firing trigger keys consumed by `run_types/<trigger>.yaml` (story 04e) and by the invocation-record `trigger_reason` field per [state-persistence.md](../../../design/05-execution-layer/state-persistence.md).

## Reading

- `docs/design/configuration-management.md` § `scheduler.yaml` — schema and worked example
- `docs/architecture/infrastructure.md` § Scheduling — APScheduler configuration, max_instances semantics, overlap-dedup
- `docs/design/README.md` § Pipeline scheduling — narrative description of the six trigger types
- `src/alphamind/config/models/collector_schedule.py` (after story 02 lands) — existing cron validation regex (`_CRON_FIELD_RE`) and `_validate_cron` helper, both reusable in this story
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/scheduler.yaml` populated with the design-doc worked example, covering all six trigger types from `design/README.md` § Pipeline scheduling:
  - `market_hours_rolling`, `off_hours_rolling`, `pre_open`, `pre_close`, `weekend_saturday`, `weekend_sunday`
  - `timezone: US/Eastern`
  - `max_instances: 1`
  - `overlap_dedup_lookback_minutes: 30`
- `src/alphamind/config/models/scheduler.py` defining:
  - `SchedulerConfig` (BaseModel: `timezone: str`, `max_instances: int = Field(ge=1)`, `overlap_dedup_lookback_minutes: int = Field(ge=0)`, `triggers: dict[str, str]` — keys are trigger names, values are cron expressions)
- A field validator on `triggers` that runs every value through the existing `_validate_cron` helper from `models/collector_schedule.py`. Import the helper rather than re-implementing.
- A field validator on `triggers` that asserts the map is non-empty and that every key matches `^[a-z][a-z0-9_]*$` (snake_case identifiers used as filename stems for `run_types/`).
- Re-export `SchedulerConfig` from `models/__init__.py`.
- Unit tests covering: shipped `config/scheduler.yaml` parses cleanly; a malformed cron expression raises; an empty `triggers:` map raises; an upper-case trigger key raises.

Out of scope:
- Cross-reference invariant — every trigger key must have a matching `run_types/{key}.yaml` file (story 06a).
- APScheduler integration — wiring the scheduler to actually fire on these triggers is owned by the pipeline-scheduler feature (not yet scoped).
- Validating that `timezone` is a known IANA zone — that requires `zoneinfo`, which is a runtime concern. Type-check as `str` only.
- Wiring `SchedulerConfig` into the loader aggregate (story 08).

## Notes

The cron-validator helper at `models/collector_schedule.py` (post-story-02) accepts five-field cron expressions. The same shape works for `scheduler.yaml` — both files describe APScheduler triggers. Reuse it; do not copy.

The trigger-name regex `^[a-z][a-z0-9_]*$` is tight on purpose. The names become filename stems (`run_types/pre_open.yaml`); a hyphen or capital letter in the YAML key would land in a filename that fails the cross-reference check at composition time. Catching this early in the model produces clearer errors than a downstream "file not found".

The `triggers:` map is `dict[str, str]` rather than typed by a closed enum because the design doc treats trigger names as operator-extensible (a future trigger like `monthly_review` would land here without a code change). The closed-enum effect is achieved at composition time when the resolver loads `run_types/{firing_trigger}.yaml` — an unknown trigger key produces a "no such run_types file" error.

Use `model_config = ConfigDict(frozen=True)`.

## Acceptance criteria

- [ ] `config/scheduler.yaml` exists, declares `timezone`, `max_instances`, `overlap_dedup_lookback_minutes`, and a `triggers:` map carrying entries for `market_hours_rolling`, `off_hours_rolling`, `pre_open`, `pre_close`, `weekend_saturday`, `weekend_sunday`.
- [ ] Each cron expression in the shipped file is a valid five-field cron string.
- [ ] `src/alphamind/config/models/scheduler.py` defines `SchedulerConfig`.
- [ ] `models/__init__.py` re-exports `SchedulerConfig`.
- [ ] A unit test asserts the shipped `config/scheduler.yaml` parses and exposes six trigger keys.
- [ ] A unit test asserts a malformed cron expression in any trigger raises `ValidationError`.
- [ ] A unit test asserts an empty `triggers:` map raises `ValidationError`.
- [ ] A unit test asserts a trigger key containing a hyphen (`pre-open`) or capital letter (`Pre_Open`) raises `ValidationError`.
- [ ] A unit test asserts `max_instances: 0` raises `ValidationError`.
- [ ] The cron-validation logic is imported from `models/collector_schedule.py`, not copied.
- [ ] `model_config = ConfigDict(frozen=True)` is set on `SchedulerConfig`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
