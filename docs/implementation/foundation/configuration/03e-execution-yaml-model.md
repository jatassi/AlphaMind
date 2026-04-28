---
status: in_progress
completed_date:
commit_id:
---

# 03e — `execution.yaml` + Pydantic model

## Goal

Land `config/execution.yaml` (execution-layer behavior knobs that aren't venue-dictated) and a Pydantic model. The file carries the operator-tunable parameters consumed by the continuous monitor (greeks refresh cadence), the guardrail-evaluation library (delta buffer), the broker adapter (submission retry window), and the paper-evaluation harness (impact coefficients, spread buffer, P/L margin).

## Reading

- `docs/design/configuration-management.md` § `execution.yaml` — schema and worked example
- `docs/design/05-execution-layer/architecture.md` § Continuous monitor — `greeks_refresh.scheduled_interval_minutes`, `move_trigger_pct` consumers
- `docs/design/06-risk-guardrails/guardrail-evaluation.md` — `conservative_delta_buffer_pct` consumer
- `docs/design/05-execution-layer/broker-adapter.md` — `submission_retry_window_seconds` consumer (Phase 2 write path retry/abandon policy)
- `docs/design/05-execution-layer/paper-evaluation-harness.md` — `paper_harness.spread_buffer_pct`, `impact_coefficients`, `pl_target_margin_pct` consumers
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/execution.yaml` populated with the design-doc worked example values:
  - `greeks_refresh.scheduled_interval_minutes: 15`
  - `greeks_refresh.move_trigger_pct: 2.0`
  - `conservative_delta_buffer_pct: 10`
  - `submission_retry_window_seconds: 30`
  - `paper_harness.spread_buffer_pct: 10`
  - `paper_harness.impact_coefficients: { market: 0.5, limit: 0.25, stop: 0.75 }`
  - `pl_target_margin_pct: 5`
- `src/alphamind/config/models/execution.py` defining:
  - `OrderType` (StrEnum: `market`, `limit`, `stop`)
  - `GreeksRefresh` (BaseModel: `scheduled_interval_minutes: int = Field(ge=1)`, `move_trigger_pct: float = Field(gt=0)`)
  - `PaperHarness` (BaseModel: `spread_buffer_pct: float = Field(ge=0)`, `impact_coefficients: dict[OrderType, float]`)
  - `ExecutionConfig` (BaseModel: `greeks_refresh: GreeksRefresh`, `conservative_delta_buffer_pct: float = Field(ge=0)`, `submission_retry_window_seconds: int = Field(ge=1)`, `paper_harness: PaperHarness`, `pl_target_margin_pct: float = Field(ge=0)`)
- A model validator on `PaperHarness` asserting that `impact_coefficients` covers every member of `OrderType` exactly. Missing entries or extra keys raise.
- A field validator on `impact_coefficients` values asserting each is `> 0` (zero-impact is suspicious; negative is unphysical).
- Re-export `ExecutionConfig`, `GreeksRefresh`, `PaperHarness`, `OrderType` from `models/__init__.py`.
- Unit tests covering: shipped `config/execution.yaml` parses cleanly; missing required field raises; negative `conservative_delta_buffer_pct` raises; `impact_coefficients` missing the `stop` key raises; `impact_coefficients` carrying an unknown order type raises.

Out of scope:
- Coupling between `greeks_refresh.scheduled_interval_minutes` and the monitor's polling cadence — the model carries the value, the monitor applies it.
- Engineering rationale checks (e.g., `conservative_delta_buffer_pct ≤ 50` is reasonable but not enforced) — these are operator judgment, not structural invariants.
- Wiring `ExecutionConfig` into the loader aggregate (story 08).

## Notes

The `OrderType` enum's three members are dictated by the paper-evaluation-harness spec. Adding a new order type later (e.g., `trailing_stop`) requires a coordinated change to both the enum and the harness; treat the enum as the single source of truth so the model validator catches half-applied changes.

`scheduled_interval_minutes` is `int` (whole minutes) per the design doc. Sub-minute intervals are not in scope and would create polling-loop pressure.

`move_trigger_pct` is a percentage (`2.0` means 2%). Field type is `float` because operators may want fractional thresholds (e.g., `1.5`). The `gt=0` constraint forbids zero (a zero threshold would fire continuously).

`conservative_delta_buffer_pct` is `float`; the design doc shows `10` but operators may set `7.5` etc. Accept fractional values.

`pl_target_margin_pct` per the design doc tightens advisory P/L targets in the paper harness. The model carries it; the harness applies it.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/execution.yaml` exists with the keys and values described in Scope.
- [ ] `config/execution.yaml` parses cleanly via `yaml.safe_load` and validates against `ExecutionConfig`.
- [ ] `src/alphamind/config/models/execution.py` defines `OrderType`, `GreeksRefresh`, `PaperHarness`, `ExecutionConfig`.
- [ ] `models/__init__.py` re-exports the four names.
- [ ] A unit test asserts the shipped `config/execution.yaml` parses and exposes `greeks_refresh.scheduled_interval_minutes == 15`.
- [ ] A unit test asserts a missing `paper_harness.impact_coefficients.stop` entry raises `ValidationError`.
- [ ] A unit test asserts an `impact_coefficients` map carrying an unknown order type (e.g., `trailing_stop`) raises `ValidationError`.
- [ ] A unit test asserts an `impact_coefficients.market: 0` value raises `ValidationError`.
- [ ] A unit test asserts a negative `conservative_delta_buffer_pct` raises `ValidationError`.
- [ ] A unit test asserts a zero `greeks_refresh.move_trigger_pct` raises `ValidationError`.
- [ ] A unit test asserts a missing required field (e.g., `pl_target_margin_pct`) raises `ValidationError`.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
