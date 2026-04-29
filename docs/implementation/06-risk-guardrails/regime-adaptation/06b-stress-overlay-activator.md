---
status: in_progress
completed_date:
commit_id:
---

# 06b — Stress overlay activator

## Goal

Land the pure function that decides whether the `stress` overlay is active for the current invocation by reading the `alert_active` flag on the most-recent `DistillationCompositeState` rows for `funding_stress` and `market_liquidity`. The stress overlay tightens exposure-related limits when the distillation layer detects funding or liquidity distress before it manifests as a VIX-level regime change. Activation is purely a function of upstream alert flags; no event-calendar lookup, no cron computation.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Distillation layer anomaly alerts — the activation contract: funding stress composite or market-liquidity score breaches alert thresholds → tighten by 15% for the alert duration
- `config/overlays/stress.yaml` — the overlay's parameters: `activation.triggers: [funding_stress_composite, market_liquidity_score]`; `multipliers` tighten `sector_concentration_pct`, `net_long_pct`, `net_short_pct`, `gross_exposure_pct` by 0.85
- `docs/design/02-distillation-layer/external.md` § 4 — composite computation; `funding_stress` and `market_liquidity` are the two composite kinds the distillation layer maintains
- `docs/design/02-distillation-layer/threshold-calibration.md` § Static configuration thresholds — the `funding_stress_component_alert_count: 2`, `funding_stress_component_percentile: 90`, `market_liquidity_alert_percentile: 10` thresholds drive the upstream alert classification (not consumed here; the upstream layer encodes them)
- `src/alphamind/persistence/models.py` § `DistillationCompositeState` — the table the activator reads (`composite_kind`, `as_of`, `alert_active`, `calibration_state`)
- `src/alphamind/persistence/models.py` § `_COMPOSITE_KINDS = ("funding_stress", "market_liquidity")` — the two composite-kind vocabulary values
- `src/alphamind/config/models/overlays.py` § `StressTrigger` StrEnum — `funding_stress_composite`, `market_liquidity_score` (the overlay-config-side names; this story bridges the two name conventions)
- `02-package-skeleton-and-types.md` — `OverlayActivationDecision` is the activator's output

## Depends on

- 02 (package skeleton + types)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/stress_activator.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_stress_activator.py`.

### 1. Public function

```python
def evaluate_stress_overlay(
    *,
    funding_stress_alert_active: bool,
    market_liquidity_alert_active: bool,
    funding_stress_calibration_state: CalibrationState,
    market_liquidity_calibration_state: CalibrationState,
    stress_overlay: StressOverlay,
) -> OverlayActivationDecision
```

The activator does not read the database directly — it accepts the four scalar inputs and the overlay config. The orchestrator (09) handles the database read via a thin helper (defined here, see §3) that selects the most-recent row per composite kind and extracts the four scalars.

### 2. Activation rules

The overlay is active iff:
- (`funding_stress_alert_active is True` AND `funding_stress_composite` is in `stress_overlay.activation.triggers`) OR
- (`market_liquidity_alert_active is True` AND `market_liquidity_score` is in `stress_overlay.activation.triggers`)

Both flags can be true simultaneously; the overlay still activates once (no stacking).

If a flag is `True` but its calibration state is `BOOTSTRAP` or `UNAVAILABLE`, the activator does not activate on that flag alone — bootstrap or unavailable composites carry insufficient confidence to trigger overlay tightening. If the *other* flag is `True` and `CALIBRATED`, the overlay activates on the other.

If both flags are `True` but both are `BOOTSTRAP` / `UNAVAILABLE`, the overlay does not activate; the rationale string surfaces this as `"Stress signals present but calibration insufficient (...)"`.

### 3. Database-read helper

```python
def fetch_composite_alert_state(
    session: Session,
) -> CompositeAlertState
```

`CompositeAlertState` is the canonical typed record declared in `02-package-skeleton-and-types.md` § 2i. Imported here:

```python
from alphamind.risk_guardrails.regime_adaptation.types import CompositeAlertState
```

Canonical fields: `funding_stress_alert_active: bool`, `funding_stress_calibration_state: CalibrationState`, `market_liquidity_alert_active: bool`, `market_liquidity_calibration_state: CalibrationState`, `funding_stress_as_of: str | None`, `market_liquidity_as_of: str | None`.

Implementation:
- For each composite kind in `("funding_stress", "market_liquidity")`, select the most-recent `DistillationCompositeState` row (ordered by `as_of` descending, limit 1).
- If a row exists, read `alert_active` (0 or 1 → bool) and `calibration_state` (string → `CalibrationState` enum).
- If no row exists for a composite kind, treat `alert_active=False` and `calibration_state=CalibrationState.UNAVAILABLE`. The activator then does not activate on the missing composite.

### 4. Computing `rationale`

```python
def _compute_rationale(
    *,
    funding_stress_alert_active: bool,
    market_liquidity_alert_active: bool,
    funding_stress_calibration_state: CalibrationState,
    market_liquidity_calibration_state: CalibrationState,
    activation_decision: bool,
) -> str
```

When `activation_decision=True`, list the contributing flag(s) and their calibration:
- `"Stress overlay active: funding_stress alert (CALIBRATED)"`
- `"Stress overlay active: market_liquidity alert (CALIBRATED)"`
- `"Stress overlay active: funding_stress alert (CALIBRATED), market_liquidity alert (CALIBRATED)"`

When `activation_decision=False` and at least one flag was `True` but uncalibrated:
- `"Stress signals present but calibration insufficient (funding_stress: BOOTSTRAP, market_liquidity: UNAVAILABLE)"`

When `activation_decision=False` and no flag is `True`: empty string (matches the no-activation convention from 06a).

### 5. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_stress_activator.py`:

- **No alerts:** both flags `False`, both `CALIBRATED`. Returns `is_active=False`, `pre_event_block_new_positions=False`, `rationale=""`.
- **Funding-stress alert only, calibrated:** `funding_stress_alert_active=True`, `market_liquidity_alert_active=False`, both `CALIBRATED`. Returns `is_active=True`, rationale mentions "funding_stress alert (CALIBRATED)".
- **Market-liquidity alert only, calibrated:** symmetric to above. Returns `is_active=True`, rationale mentions "market_liquidity alert (CALIBRATED)".
- **Both alerts active, both calibrated:** Returns `is_active=True`, rationale mentions both alerts.
- **Single alert, BOOTSTRAP calibration:** `funding_stress_alert_active=True`, `funding_stress_calibration_state=BOOTSTRAP`, others false/calibrated. Returns `is_active=False`, rationale mentions "Stress signals present but calibration insufficient".
- **Single alert, UNAVAILABLE calibration:** symmetric. Returns `is_active=False`.
- **Both alerts active, one bootstrap:** `funding_stress_alert_active=True (CALIBRATED)`, `market_liquidity_alert_active=True (BOOTSTRAP)`. Returns `is_active=True` because the calibrated alert alone triggers; rationale mentions only "funding_stress alert (CALIBRATED)".
- **Trigger absent from `stress_overlay.activation.triggers`:** synthetic overlay config with `triggers=[funding_stress_composite]` only. `market_liquidity_alert_active=True`, calibrated. Returns `is_active=False` (the market_liquidity trigger is not in the overlay's activation list).
- **Returned `OverlayActivationDecision.overlay` is always `Overlay.stress`** regardless of activation outcome.
- **Returned `OverlayActivationDecision.pre_event_block_new_positions` is always `False`** (this field is pre-event-overlay-specific; the stress overlay never sets it true).
- **`fetch_composite_alert_state` happy path:** insert a `DistillationCompositeState` row for `funding_stress` and `market_liquidity` via fixture; assert the helper returns the correct field values.
- **`fetch_composite_alert_state` with multiple rows per kind:** insert two `funding_stress` rows with different `as_of`; assert the helper returns the most recent.
- **`fetch_composite_alert_state` with missing composite:** insert only a `funding_stress` row; the helper returns `market_liquidity_alert_active=False, market_liquidity_calibration_state=UNAVAILABLE, market_liquidity_as_of=None`.
- **`fetch_composite_alert_state` with empty table:** both kinds return `False, UNAVAILABLE, None`.
- **Determinism / purity (activator):** identical inputs produce identical outputs.

Out of scope:

- The pre-event overlay activator — story 06a.
- Computing the *funding stress composite* or the *market liquidity score* — that is the distillation layer's responsibility (the upstream layer that writes to `DistillationCompositeState`).
- Defining what "alert-active" means in terms of the underlying components (component count breaching percentile thresholds) — encoded by the distillation layer per `threshold-calibration.md`. This story consumes the boolean.
- Subscribing to a real-time stream of distillation alerts. The activator reads the latest persisted row at invocation start; the orchestrator (09) is invocation-driven, not stream-driven.
- Hysteresis on stress activation (e.g., "only deactivate after the alert has been clear for N invocations"). The activation flag follows the upstream alert flag directly; if hysteresis is needed, it lives in the upstream composite computation.

## Notes

The two name conventions — composite kinds (`funding_stress`, `market_liquidity` — used in `_COMPOSITE_KINDS` and the database) vs. overlay triggers (`funding_stress_composite`, `market_liquidity_score` — used in `StressTrigger`) — are bridged inside the activator. The mapping is one-to-one and lives as a private dict constant:

```python
_COMPOSITE_KIND_TO_OVERLAY_TRIGGER: Mapping[str, StressTrigger] = MappingProxyType({
    "funding_stress": StressTrigger.funding_stress_composite,
    "market_liquidity": StressTrigger.market_liquidity_score,
})
```

The duplication of names across the two layers reflects independent naming conventions in the distillation and overlay configs; this activator is the seam.

Per `feedback_no_inventing_component_names.md`, both name sets are pre-existing. The activator does not introduce a third convention.

Per `feedback_simplify_before_building.md`, the activator does not implement a "minimum alert duration" or "stress severity scoring" beyond the binary flag. The design specifies binary activation with the same multipliers regardless of severity; if severity-dependent multipliers are needed later, they land as overlay-config additives.

Per `feedback_avoid_numeric_anchors.md`, the activator has no numeric thresholds. Calibration-state classification is the only conditional branch, and the `CalibrationState` enum's vocabulary (CALIBRATED / BOOTSTRAP / UNAVAILABLE) is the existing distillation convention.

Per `feedback_per_producer_schema.md`, the activator returns the same `OverlayActivationDecision` shape as the pre-event activator. Same record; one overlay-specific field (`pre_event_block_new_positions`) is unused by this overlay and always set `False`.

The decision to skip activation when the alert is uncalibrated is conservative: tightening exposure on a possibly-spurious upstream signal is worse than not tightening on a genuine signal that hasn't yet calibrated. The downstream rationale string makes the suppression visible to the operator dashboard so that long-running BOOTSTRAP states can be addressed by accelerating data accumulation.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/stress_activator.py` exists and defines `evaluate_stress_overlay`, `fetch_composite_alert_state`, `_COMPOSITE_KIND_TO_OVERLAY_TRIGGER`. `CompositeAlertState` is imported from `alphamind.risk_guardrails.regime_adaptation.types` (canonical declaration in story 02); not redeclared here.
- [ ] `evaluate_stress_overlay` and `fetch_composite_alert_state` are re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`. (`CompositeAlertState` is already re-exported via story 02.)
- [ ] No alerts active returns `is_active=False`, `pre_event_block_new_positions=False`, `rationale=""`.
- [ ] Single calibrated alert (either kind) activates the overlay; rationale mentions the kind and "CALIBRATED".
- [ ] Both calibrated alerts active returns `is_active=True`; rationale mentions both.
- [ ] Alert with BOOTSTRAP or UNAVAILABLE calibration alone does not activate; rationale mentions "calibration insufficient".
- [ ] Calibrated alert + uncalibrated alert: activation succeeds based on the calibrated alert.
- [ ] Trigger absent from `stress_overlay.activation.triggers` does not contribute to activation.
- [ ] Returned `OverlayActivationDecision.overlay` is always `Overlay.stress`.
- [ ] Returned `OverlayActivationDecision.pre_event_block_new_positions` is always `False`.
- [ ] `fetch_composite_alert_state` returns the most-recent row per composite kind and treats missing composites as `(False, UNAVAILABLE, None)`.
- [ ] The activator function is pure: equal inputs produce equal outputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
