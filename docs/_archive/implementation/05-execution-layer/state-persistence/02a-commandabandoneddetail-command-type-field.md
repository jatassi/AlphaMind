# 02a — CommandAbandonedDetail.command_type field + projection update

## Goal

Extend `CommandAbandonedDetail` in `src/alphamind/portfolio_state/events/activity_log.py` with a `command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]` field, then update `StrategistAbandonedAction` in `src/alphamind/portfolio_state/consumers/strategist.py` to thread the value through instead of defaulting to `"ADD"`. The field is required for the strategist's "Abandoned position actions" guardrail-state-header section to surface the right per-command-type framing to the operator. Independent of the SQL persistence work — runs in parallel with story 02b.

## Reading

* `src/alphamind/portfolio_state/events/activity_log.py` § `CommandAbandonedDetail` — the typed-record this story extends; sits next to the rest of the event-detail family
* `src/alphamind/portfolio_state/consumers/strategist.py` § `StrategistAbandonedAction` — the projection that currently defaults `command_type` to `"ADD"`. Find every site reading the projection and verify the consumer pattern
* `docs/design/05-execution-layer/state-persistence.md` § Activity log entries / PM decision events — the `command_abandoned` event detail spec naming the originating-agent and per-command-type fields
* `docs/design/06-risk-guardrails/state-delivery.md` § Strategist guardrail state header — the consumer of `StrategistAbandonedAction` (the "Abandoned position actions" block); confirm the projection's `command_type` is rendered in the operator-facing output
* `src/alphamind/decision/portfolio_manager/__init__.py` — names the five PM command types (`OPEN`, `CLOSE`, `ADD`, `ADJUST`, `CANCEL`); the Literal must mirror these
* Parent issue [ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119) Pre-resolved decision (G) — context for why this fix lives in this work tree

## Depends on

* <issue id="71c92bc8-3b67-473b-9c34-82652af670df">ALP-354</issue> (this work tree, story 01) — package skeleton landed so the work tree's commit history starts coherently. (No code dependency, but landing 01 first preserves dispatch ordering.)

## Scope

In scope: typed-record edit + projection edit + tests. No SQL changes (the activity log SQL persistence ships in story 03 and reads the typed shape at that time).

### 1\. `CommandAbandonedDetail.command_type` field

Add a new required field to `CommandAbandonedDetail`:

```python
command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]
```

Mirrors the five PM command kinds. The field is non-nullable — every command-abandoned event has a known command type at emission time. No default value (forces emitters to supply it).

### 2\. Update existing emitters

Find every site that constructs `CommandAbandonedDetail` and supply the `command_type` argument. The current site graph (verify before editing — files may have moved):

* `src/alphamind/execution/oms/submit_envelope_mcp.py` — if it constructs a `CommandAbandonedDetail`, supply the right command_type from the OMSCommand union member.
* `tests/portfolio_state/events/...` — fixture builders constructing detail records. Update each to pass a representative command_type.

### 3\. `StrategistAbandonedAction` projection

In `src/alphamind/portfolio_state/consumers/strategist.py`, locate `StrategistAbandonedAction` and remove the hard-coded `command_type = "ADD"` default. Read the value from the source `CommandAbandonedDetail` instead.

### 4\. Tests

Update `tests/portfolio_state/events/test_activity_log.py` (or the equivalent file) to add a test that constructs `CommandAbandonedDetail` with each of the five command_type values and asserts they round-trip through Pydantic. Update `tests/portfolio_state/consumers/test_strategist.py` (or equivalent) to add a test asserting the projection threads `command_type` through from the detail record.

### Out of scope

* No SQL schema or migrations — the activity_log table doesn't exist yet (story 03).
* No `command_type` column in any SQL table — the field rides as part of the JSON detail payload story 03 stores.
* No changes to other event detail records.

## Acceptance criteria

- [ ] `CommandAbandonedDetail.command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]` is a required field on the typed record.
- [ ] Constructing `CommandAbandonedDetail` without supplying `command_type` raises a Pydantic `ValidationError`.
- [ ] Each of the five Literal values round-trips through `model_dump_json()` / `model_validate_json()` without information loss.
- [ ] `StrategistAbandonedAction` no longer hard-codes `command_type = "ADD"` and instead reads from the source `CommandAbandonedDetail`.
- [ ] Every existing call site constructing `CommandAbandonedDetail` (production and test) supplies a `command_type` argument.
- [ ] `tests/portfolio_state/events/test_activity_log.py` (or equivalent) covers all five command_type values via a parametrised test.
- [ ] `tests/portfolio_state/consumers/test_strategist.py` (or equivalent) asserts the projection threads `command_type` through.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/portfolio_state/ -n auto` passes (the broader portfolio_state test surface — verifies no regression).

## Verification

Run `uv run pytest tests/portfolio_state/ -n auto` and confirm the parametrised round-trip test plus the strategist projection test pass alongside the existing portfolio-state test surface (no regression). Spot-check the rendered "Abandoned position actions" block in any strategist guardrail-state-header fixture to confirm `command_type` now reflects the source event detail.