# 06a — Refactor `portfolio_state/events/activity_log.py` (split + Pydantic→dataclass + Money)

## Goal

Refactor the 1033-LOC `portfolio_state/events/activity_log.py` god module along three coordinated axes in one story: (1) split the file into per-event-group submodules; (2) convert all 38 detail-payload Pydantic classes to `@dataclass(frozen=True, slots=True)` with `__post_init__` invariants; (3) migrate the 16 money fields to `Money` from `_kernel/money.py`. All three changes touch the same codec layer (`execution.state_persistence.tables.activity_log_codec`), so sequencing them across stories would risk one half breaking the other.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L8 (god module) + L5 (Pydantic internal) + L4 (money) all converge here
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decision (F) — single-story rationale
* `src/alphamind/portfolio_state/events/activity_log.py` — 1033 LOC, 38 detail classes, 2 dispatch dicts
* `src/alphamind/execution/state_persistence/tables/activity_log_codec.py` — the codec layer this story coordinates with
* Story 05b (<issue id="2852122e-7fa0-4d6f-8f52-9bf285a44fb8">ALP-462</issue>) — Money/Price types ready; codec round-trip pattern proven
* `.claude/skills/python-architecture/references/data-and-types.md` § D1 (Pydantic vs frozen dataclass)

## Depends on

* 05b (<issue id="2852122e-7fa0-4d6f-8f52-9bf285a44fb8">ALP-462</issue>) — Money primitive boundary migration must land first so the codec round-trip pattern is established

## Scope

In scope: structural decomposition of `activity_log.py`; Pydantic→frozen-dataclass conversion of all 38 detail classes + `ActivityLogEntry`; migration of 16 money fields to `Money`; corresponding codec updates; test updates. Tests at `tests/portfolio_state/events/`.

### 1\. Split by event group

Create per-event-group submodules under `src/alphamind/portfolio_state/events/`:

* `events/position_lifecycle.py` — `POSITION_OPENED`, `POSITION_CLOSED`, `POSITION_ADDED`, `POSITION_REDUCED` detail classes
* `events/order_lifecycle.py` — `ORDER_SUBMITTED`, `ORDER_FILLED`, `ORDER_PARTIALLY_FILLED`, `ORDER_CANCELLED`, `ORDER_EXPIRED`, `ORDER_REJECTED`, `ORDER_MODIFIED` details
* `events/bracket.py` — `BRACKET_ACTIVATED`, `BRACKET_COMPLETED`, `BRACKET_DISSOLVED`, `BRACKET_MODIFIED`, `BRACKET_INCOMPLETE_WARNING`, `BRACKET_CANCELLED_CORPORATE_ACTION` details
* `events/thesis.py` — `THESIS_CREATED`, `THESIS_COMPONENT_ADDED`, `THESIS_COMPONENT_UPDATED`, `THESIS_RESOLVED`, `THESIS_STATUS_CHANGED` details
* `events/cash_margin.py` — `CASH_DEBITED`, `CASH_CREDITED`, `CAPITAL_RESERVED`, `CAPITAL_RELEASED`, `MARGIN_CALL`, `MARGIN_CALL_RESOLVED`, `MARGIN_LIQUIDATION` details
* `events/risk_guardrail.py` — `GUARDRAIL_REJECTION`, `RISK_LIMIT_APPROACHED`, `RISK_PARAMETER_CHANGED`, `EMERGENCY_INVOCATION_REQUESTED`, `HALT_ACTIVATED`, `HALT_LIFTED`, `GREEKS_REFRESH_FAILED` details
* `events/pm_decision.py` — `PM_DECISION`, `COMMAND_ABANDONED`, `ENVELOPE_PARSE_FAILED`, `ENVELOPE_REJECTED` details
* `events/corporate_action.py` — `CORPORATE_ACTION_APPLIED` detail
* `events/reconciliation.py` — `RECONCILIATION_ALERT` detail
* `events/configuration.py` — `DISTILLATION_CONFIG_CHANGE` detail
* `events/types.py` — `EventGroup`, `EventType`, `EventSource`, `PositionExitMethod`, etc. enums (the shared vocabulary); plus `ActivityLogEntry` (the umbrella record)
* `events/__init__.py` — curated re-exports of every public symbol; `__all__` populated; backward-compat shim if needed

### 2\. Auto-generate dispatch dicts from a single registry

Replace the two hand-maintained dispatch dicts (`EVENT_TYPE_TO_DETAIL_CLASS`, `EVENT_TYPE_TO_GROUP`) with derivation from a single registry list per submodule:

```python
# events/position_lifecycle.py
_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (EventType.POSITION_OPENED, PositionOpenedDetail, EventGroup.POSITION_LIFECYCLE),
    ...
]
```

`events/__init__.py` aggregates: `EVENT_TYPE_TO_DETAIL_CLASS = {e: c for module in [...] for (e, c, _) in module._REGISTRY}`. The runtime `_validate_detail_class` validator stays but becomes redundant — keep as a defense-in-depth or remove with operator nod.

### 3\. Convert detail classes from Pydantic to frozen dataclass

For each detail class:

```python
# Before:
class PositionOpenedDetail(BaseModel):
    model_config = {"frozen": True}
    fill_price: float
    ...

# After:
@dataclass(frozen=True, slots=True)
class PositionOpenedDetail:
    fill_price: Price  # was float
    ...
    def __post_init__(self) -> None:
        # invariants the Pydantic validators expressed
        ...
```

### 4\. Migrate 16 money fields

Fields per audit hot-list: `fill_price` (203), `exit_price` (227), `realized_pnl_usd` (228), `fees` (274), `amount_usd` (458, 468, 479, 488), `margin_required_usd` (497), `deficit_usd` (499), `liquidation_price` (516), `loss_usd` (517), plus the rest. Migrate `float → Money` or `float → Price` per field semantics.

### 5\. Update codec

`execution/state_persistence/tables/activity_log_codec.py` round-trips `ActivityLogEntry` ↔ SQLite. Update the codec to:

* Read frozen-dataclass instances on the encode path
* Write frozen-dataclass instances on the decode path
* Preserve `Money`/`Price` precision (Decimal as str in SQLite TEXT column, or NUMERIC)
* Use the per-event-group dispatch to instantiate the correct detail subclass

### 6\. Update consumers

Every consumer of `portfolio_state.events.activity_log` retargets to either the new per-group module or the curated `events/__init__.py` re-export. Backward-compat: `events/__init__.py` keeps the existing import surface so external consumers don't all retarget at once.

### Out of scope

The activity_log codec's broader audit (sidecar JSON encoding for forensics, etc.) is its own concern — this story updates what the type changes require, not unrelated codec refactors.

## Acceptance criteria

- [ ] `portfolio_state/events/activity_log.py` is split into 10+ per-event-group submodules + `events/types.py` + curated `events/__init__.py`.
- [ ] Every detail class is `@dataclass(frozen=True, slots=True)` with `__post_init__` invariants; no `BaseModel` remains under `portfolio_state/events/`.
- [ ] All 16 money fields use `Money` or `Price` from `_kernel.money`.
- [ ] Dispatch dicts (`EVENT_TYPE_TO_DETAIL_CLASS`, `EVENT_TYPE_TO_GROUP`) are derived from per-module registries; no hand-maintained dicts remain.
- [ ] `execution/state_persistence/tables/activity_log_codec.py` round-trips frozen-dataclass instances; precision preserved on Money fields.
- [ ] Backward-compat: existing `from alphamind.portfolio_state.events.activity_log import X` patterns continue to work via `events/__init__.py` re-export (or all consumers updated — operator preference).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

Codec round-trip test: instantiate every `*Detail` class via Hypothesis or fixture; serialize via codec; deserialize; assert exact equality including Money precision. Spot-check: a fill of `money("100.50")` at `money("0.001")` in fees serializes, deserializes, and the activity log row's reconstructed `OrderFilledDetail.fees` equals exactly `money("0.001")`. `grep -rn "BaseModel" src/alphamind/portfolio_state/events/` returns zero hits.