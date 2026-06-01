# 05j — View E — Risk dashboard + regime timeline + calibration mix

## Goal

Ship view group E in one combined story: the guardrail dashboard (per-rule status, multipliers / overlays, drawdown state, recent breaches), the regime / overlay timeline (chronological regime classifications + transitions + overlay activations with P/L drawdown + breach event markers), and the calibration mix view (per-invocation distillation calibration-state reduction with stacked-segment bar + trailing 7-day trend + stuck-in-non-calibrated alert).

## Reading

* `docs/design/command-center.md` § Guardrail dashboard, § Regime and overlay timeline, § Calibration mix.
* `docs/design/06-risk-guardrails/rules-and-limits.md` — guardrail rule registry rendered per-rule.
* `docs/design/06-risk-guardrails/breach-behavior.md` — breach response classification + halt mode + drawdown progressive tiers.
* `docs/design/02-distillation-layer/threshold-calibration.md` § Calibration-state snapshot file — `data/provenance/invocations/<id>/data_calibration_state.json` schema the calibration mix view reads.
* `src/alphamind/state/tables/positions.py`, `drawdown_state.py` — state the dashboard reads.
* `src/alphamind/risk_guardrails/` (or wherever the guardrail evaluation library lives) — per-rule evaluation primitives the dashboard uses to compute current-value + headroom per rule.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/risk.py`. Frontend at `frontend/src/routes/_authed/risk/` (three child routes: `index.tsx` dashboard, `timeline.tsx`, `calibration-mix.tsx`) + `frontend/src/views/risk/` per-view components. Tests at `tests/command_center/views/test_risk.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* `GET /api/views/risk/guardrail-dashboard` — per-rule status (rule_name, current_value, limit_value, headroom_pct, zone normal / warning 70–85% / critical 85–95% / hard-block ≥95%); active multipliers + overlays (current regime label + multiplier table + active overlays + mode flag); drawdown state (daily + cumulative progressive tier + halt-mode flag); recent breaches (activity_log filtered for `guardrail_rejection` / `risk_limit_approached` / `risk_parameter_changed` last 24h).
* `GET /api/views/risk/regime-timeline?from=&to=` — chronological list of regime classifications + transitions + overlay activations + P/L drawdown markers + breach event markers within the time window.
* `GET /api/views/risk/calibration-mix?invocations_window=` — per-invocation calibration-state reductions from `data/provenance/invocations/{id}/data_calibration_state.json` files for the trailing-N-invocations window; plus trailing 7-day mix trend + stuck-in-non-calibrated entries with last-calibrated invocation per `<block_id>`.

### 2\. Frontend

`/risk` route — guardrail dashboard with per-rule rows; click → drill-down showing per-position contribution + breach response classification + cross-ref to rules-and-limits.md.

`/risk/timeline` route — Recharts time-series with regime band overlay + breach event markers + drawdown trace.

`/risk/calibration-mix` route — Recharts stacked-segment bar per invocation + 7-day stacked-area trend + stuck-in-non-calibrated panel-level warning list with cross-reference to the warm-up duration estimate from threshold-calibration.md.

Per-rule drill-down side panel renders per-position contribution + breach response classification.

### 3\. Vitest tests

Each view component renders documented fields. Per-rule zone classification matches thresholds. Calibration mix bar segments match per-state counts.

## Acceptance criteria

- [ ] `GET /api/views/risk/guardrail-dashboard` returns per-rule status with documented zone classification, active multipliers, drawdown, recent breaches.
- [ ] `GET /api/views/risk/regime-timeline` returns regime + overlay events within the window.
- [ ] `GET /api/views/risk/calibration-mix` reads `data/provenance/invocations/{id}/data_calibration_state.json` and returns per-invocation + 7-day trend + stuck-in-non-calibrated.
- [ ] Frontend `/risk`, `/risk/timeline`, `/risk/calibration-mix` routes render documented content.
- [ ] Per-rule drill-down panel renders per-position contribution.
- [ ] Calibration mix stuck-in-non-calibrated alerts cross-reference warm-up duration estimate.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.