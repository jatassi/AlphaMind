# 02 — Move risk-guardrail enums out of `records/capital.py`

## Goal

Fix the layering inversion where `portfolio_state/records/capital.py` defines four enums conceptually owned by the risk_guardrails layer: `RegimeLabel` (volatility regime), `RegimeTransitionState` (transition machinery), `RiskZone` (proximity classifier), `DrawdownTier` (breach-behavior tiering). Today `risk_guardrails.breach_behavior.drawdown_tiers` imports `DrawdownTier` from `portfolio_state.records.capital` — the dependency arrow points backwards. Move each enum to its conceptually-owning location under `risk_guardrails/`, update all import sites in lockstep, and have `capital.py` import the enums from their new homes (since `CashLedger`, `DrawdownState`, `RiskBudgetEntry`, and `ActiveRiskParameterSet` continue to consume them as field types).

## Reading

* `src/alphamind/portfolio_state/records/capital.py:24-56` — the four enums to relocate (`RegimeLabel`, `RegimeTransitionState`, `RiskZone`, `DrawdownTier`).
* `src/alphamind/risk_guardrails/regime_adaptation/types.py` — natural home for `RegimeLabel` and `RegimeTransitionState` (already contains regime-adaptation types per the regime-adaptation feature <issue id="8e265444-f821-43a4-8997-b1747aba1855">ALP-213</issue>).
* `src/alphamind/risk_guardrails/breach_behavior/types.py` — natural home for `DrawdownTier` (the breach-behavior feature <issue id="0249467b-39f1-4876-99da-b3ef4e00b13b">ALP-212</issue> defines drawdown-tier behavior).
* `src/alphamind/risk_guardrails/guardrail_evaluation/` — natural home for `RiskZone` (the proximity classifier the guardrail-evaluation library produces).
* `src/alphamind/risk_guardrails/breach_behavior/drawdown_tiers.py:21` — the canonical importer of `DrawdownTier` from [capital.py](http://capital.py); the inversion this story fixes.
* `src/alphamind/portfolio_state/records/thesis_quality.py:12,22` — re-exports `RegimeLabel` from capital in `__all__`; this re-export becomes a re-export from the new home (or is dropped entirely if no consumer relies on this back-channel).
* All importers found by `grep -rn "from alphamind.portfolio_state.records.capital import" src/ tests/` — list of sites needing import-path updates. Approximately ~30-50 sites.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (A) — `portfolio_state.records.*` stays the canonical home for RECORDS; this story moves only the four risk-guardrail ENUMS that don't belong there.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates.

## Scope

Source under `src/alphamind/portfolio_state/records/capital.py` (remove enum definitions; import from new locations) and three target homes under `src/alphamind/risk_guardrails/{regime_adaptation,breach_behavior,guardrail_evaluation}/types.py`. All importers across `src/` and `tests/` updated to new paths.

### 1\. Define the new homes

* `src/alphamind/risk_guardrails/regime_adaptation/types.py` — add `RegimeLabel` and `RegimeTransitionState` (verbatim from capital.py:24-39).
* `src/alphamind/risk_guardrails/breach_behavior/types.py` — add `DrawdownTier` (verbatim from capital.py:50-56).
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` (create if it doesn't exist; check first) — add `RiskZone` (verbatim from capital.py:41-48).

If any of those modules already export an enum with the same name (collision check — unlikely but possible), pause and surface to the operator.

### 2\. Update `capital.py`

Remove the four enum class definitions (capital.py:24-56). Replace with imports from the new homes:

```python
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone
from alphamind.risk_guardrails.regime_adaptation.types import (
    RegimeLabel,
    RegimeTransitionState,
)
```

The remaining types in [capital.py](http://capital.py) (`CashLedger`, `DrawdownState`, `RiskBudgetEntry`, `RiskBudgetConsumption`, `ActiveRiskParameterEntry`, `ActiveRiskParameterSet`, `UnsettledProceedsEntry`) continue to use these enum types as field types; they import from [capital.py](http://capital.py)'s new import lines.

For backward compatibility with the 30-50 existing importers, [capital.py](http://capital.py) *re-exports* the four enums (the imports above already accomplish this, since names imported into a module are accessible via that module's namespace).

### 3\. Update direct importers

Search for `from alphamind.portfolio_state.records.capital import` across `src/` and `tests/`. For each site, update only the imports that are *only* importing the relocated enums (RegimeLabel / RegimeTransitionState / RiskZone / DrawdownTier) — change the import path to the canonical home.

Sites still importing CashLedger / DrawdownState / RiskBudgetEntry / etc. from [capital.py](http://capital.py) are unchanged.

The most-touched site will be `risk_guardrails/breach_behavior/drawdown_tiers.py:21` — change `from alphamind.portfolio_state.records.capital import DrawdownTier` to `from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier`.

### 4\. Update `thesis_quality.py` re-export

`src/alphamind/portfolio_state/records/thesis_quality.py:12,22` re-exports `RegimeLabel`. Change the import line:

```python
from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel
```

The `__all__ = ["RegimeLabel", ...]` re-export stays — preserves backward compatibility for any consumer that imports `RegimeLabel` via `thesis_quality`.

### 5\. Verify no circular import

After the moves, check for import cycles. Particularly:

* `risk_guardrails.regime_adaptation.types` must not import from `portfolio_state.records.capital` (would cycle).
* `risk_guardrails.breach_behavior.types` must not import from `portfolio_state.records.capital` (same).
* `risk_guardrails.guardrail_evaluation.types` must not import from `portfolio_state.records.capital`.

The enums themselves have no portfolio_state dependencies — they're pure StrEnum members — so cycles shouldn't arise. Confirm by running the test suite.

### Out of scope

* Moving `CashLedger`, `DrawdownState`, `RiskBudgetEntry`, etc. — those records are correctly placed in `portfolio_state/records/capital.py` (they're position-state aggregates).
* Splitting `capital.py` into `cash.py` and `risk_budget.py` and `risk_parameters.py` — that's the structural-reorg story (04a / O1).
* Renaming any of the four enums — keep names identical to preserve import-line readability across all consumers.

## Acceptance criteria

- [ ] `RegimeLabel` and `RegimeTransitionState` are defined in `src/alphamind/risk_guardrails/regime_adaptation/types.py`; importable from `alphamind.risk_guardrails.regime_adaptation.types`.
- [ ] `DrawdownTier` is defined in `src/alphamind/risk_guardrails/breach_behavior/types.py`; importable from `alphamind.risk_guardrails.breach_behavior.types`.
- [ ] `RiskZone` is defined in `src/alphamind/risk_guardrails/guardrail_evaluation/types.py`; importable from `alphamind.risk_guardrails.guardrail_evaluation.types`. (If the file didn't exist, it now exists.)
- [ ] `src/alphamind/portfolio_state/records/capital.py` no longer defines these four enums — it imports them from their new homes and re-exports them via the imported-names mechanism.
- [ ] All four enums remain importable from `alphamind.portfolio_state.records.capital` (backward compatibility); `from alphamind.portfolio_state.records.capital import RegimeLabel` continues to work for legacy code paths.
- [ ] `risk_guardrails.breach_behavior.drawdown_tiers` no longer imports `DrawdownTier` from `portfolio_state.records.capital` — imports from the new canonical home.
- [ ] No new circular imports introduced — `uv run python -c "import alphamind.portfolio_state.records.capital; import alphamind.risk_guardrails.regime_adaptation.types; import alphamind.risk_guardrails.breach_behavior.types; import alphamind.risk_guardrails.guardrail_evaluation.types"` succeeds.
- [ ] All existing `uv run pytest -n auto` tests pass — no test breaks because of the import path change (relies on [capital.py](http://capital.py)'s backward-compat re-export).
- [ ] Identity checks pass: `RegimeLabel` imported from [capital.py](http://capital.py) is the same object as `RegimeLabel` imported from `regime_adaptation.types` (i.e., the re-export is the same class object, not a copy).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean across the whole repo.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes.
* Spot-check identity: `python -c "from alphamind.portfolio_state.records.capital import DrawdownTier as A; from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier as B; print(A is B)"` should print `True`.
* Spot-check no inversion: `grep -rn "from alphamind.portfolio_state.records.capital import" src/alphamind/risk_guardrails/` should return no hits except possibly `_assert_unique_rule_ids` if a guardrail-evaluation site imports it (unlikely but allowed — the helper is a true portfolio_state utility).
* Spot-check the new files exist and are non-empty: `wc -l src/alphamind/risk_guardrails/regime_adaptation/types.py src/alphamind/risk_guardrails/breach_behavior/types.py src/alphamind/risk_guardrails/guardrail_evaluation/types.py`.
* Lint clean per CLAUDE.md.