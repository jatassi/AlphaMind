# 01a — Package skeleton + IBKR-mirror v1 config

## Goal

Create the `src/alphamind/execution/regt_margin_attribution/` package with a `RegTMarginAttributionConfig` Pydantic model and the `config/regt_margin_attribution.yaml` file populated with v1 IBKR-mirror parameters. The config is the version-pinned snapshot the rest of the work tree consumes: per-symbol shock-percentage overrides (researched from IBKR's public margin-methodology disclosure where reachable), asset-class defaults, IV-shock paired multipliers, the `pm_model_version` string, and the annualised risk-free rate. Story 02 (class-group composition), 03b (PM stress class-group revaluation), 04 (PM-equivalent aggregator), and 05 (per-fill orchestrator) all consume this config; 06a's Phase 1 wedge loads it once at invocation start.

## Reading

* `docs/design/05-execution-layer/regt-margin-attribution.md` § Portfolio-margin reference model — IBKR-mirror v1 specification, shock-grid convention, version-pinning intent.
* `docs/design/05-execution-layer/regt-margin-attribution.md` § Aggregation and delivery — confirms `pm_model_version` lives in the per-fill attribution record alongside the dollar fields.
* `docs/design/05-execution-layer/venue-configuration.md` § Margin tiers — confirms Reg T per-leg percentages are *not* operator-tunable and live in code, so they do NOT go in this yaml.
* `src/alphamind/risk_guardrails/breach_behavior/config.py` — reference Pydantic config shape: frozen, finite-float annotations, named loader function.
* `src/alphamind/portfolio_state/records/cash.py` — `_FiniteFloat` annotated-type pattern this config reuses.
* `config/execution.yaml` and `config/breach_behavior.yaml` — neighbour yaml files; conventions for leading comment blocks, field ordering, decimal-form percentages.
* IBKR public margin-methodology disclosure: `https://www.interactivebrokers.com/en/trading/margin-stocks.php` and any linked margin-rates table — research source for per-symbol shock-percentage seeding.
* `ALP-126` parent issue Pre-resolved configuration decisions (A)–(D) — relevant fields, defaults, version-string convention.

## Depends on

None within this work tree. All hard sibling-tree dependencies are already *done* per the parent issue's Cross-feature dependencies section.

## Scope

In scope under `src/alphamind/execution/regt_margin_attribution/`. Tests at `tests/execution/regt_margin_attribution/`. Yaml at `config/regt_margin_attribution.yaml`.

### 1\. Package skeleton

Create `src/alphamind/execution/regt_margin_attribution/__init__.py` exporting `RegTMarginAttributionConfig` and `load_regt_margin_attribution_config` from `.config`. No other exports yet — later stories add modules and extend `__all__`.

Create `tests/execution/regt_margin_attribution/__init__.py` (empty).

### 2\. `RegTMarginAttributionConfig` Pydantic model

In `src/alphamind/execution/regt_margin_attribution/config.py`:

* `IvShockMultipliers(BaseModel)` — frozen. Fields: `worst_down_multiplier: _FinitePositiveFloat`, `worst_up_multiplier: _FinitePositiveFloat`. Validators reject `worst_down_multiplier < 1.0` (IV must rise on a down-shock) and `worst_up_multiplier > 1.0` (IV must fall or hold on an up-shock).
* `ShockParameters(BaseModel)` — frozen. Fields: `per_symbol_overrides: Mapping[str, _FinitePositiveFloat]`, `high_cap_equity: _FinitePositiveFloat`, `small_cap_equity: _FinitePositiveFloat`, `unmapped_default: _FinitePositiveFloat`. All percentages in decimal form (e.g., `0.15` for ±15%). Validator: all values in `(0.0, 1.0)`; per-symbol-override keys normalised to upper-case on construction.
* `RegTMarginAttributionConfig(BaseModel)` — frozen. Fields: `pm_model_version: str` (non-empty), `shock_parameters: ShockParameters`, `iv_shock: IvShockMultipliers`, `risk_free_rate_annual: _FiniteFloat` (negative allowed per the Black-Scholes core's convention; range `[-0.05, 0.20]`).

`_FiniteFloat` and `_FinitePositiveFloat` follow the same annotated-type pattern as `src/alphamind/portfolio_state/records/cash.py`.

### 3\. Loader

`def load_regt_margin_attribution_config(path: Path | str | None = None) -> RegTMarginAttributionConfig` reads `config/regt_margin_attribution.yaml` (or the supplied path) and returns the parsed model. Mirrors `load_breach_behavior_config` in shape. Default-path resolution follows the existing `alphamind.config` convention.

### 4\. `config/regt_margin_attribution.yaml`

Populate with the v1 snapshot:

* `pm_model_version: "ibkr_mirror_v1_2026Q2"` per parent (D).
* `risk_free_rate_annual: 0.0425` (current US 3-month T-bill yield reference; document source URL + access date in a yaml comment).
* `iv_shock.worst_down_multiplier: 1.20`, `iv_shock.worst_up_multiplier: 0.95` per parent (B).
* `shock_parameters.high_cap_equity: 0.15`, `shock_parameters.small_cap_equity: 0.20`, `shock_parameters.unmapped_default: 0.20` per parent (C) fallbacks.
* `shock_parameters.per_symbol_overrides:` — populated from IBKR's published disclosure for the minimum coverage list below.

Minimum coverage list:

* Major US-equity ETFs: SPY, QQQ, IWM, DIA, VTI, VOO.
* Sector SPDRs: XLF, XLE, XLK, XLV, XLY, XLP, XLU, XLI, XLB, XLRE, XLC.
* Top S&P 500 names by market cap: AAPL, MSFT, NVDA, GOOGL, AMZN, META, BRK.B, TSLA, JPM, UNH.

### 5\. IBKR research step

Before populating the yaml, fetch IBKR's currently published margin-methodology disclosure (start at `https://www.interactivebrokers.com/en/trading/margin-stocks.php` and follow the margin-rates table link). For each symbol in the minimum coverage list:

* If IBKR publishes a specific scenario-shock percentage, use that value (record source URL + access date in a yaml comment alongside the entry).
* If IBKR publishes only a generic asset-class shock applicable to the symbol, fall through to the appropriate asset-class default (no per-symbol entry; document the fall-through in a yaml comment).
* If IBKR's disclosure is not reachable during research, the agent falls back to design-doc defaults (`SPY/QQQ/DIA/VOO: 0.15`; `IWM: 0.20`) and omits the remaining per-symbol overrides. The leading comment block records that the snapshot is design-doc-default rather than researched.

The yaml file's leading comment block documents: (a) `pm_model_version` value, (b) IBKR disclosure URL + access date, (c) which entries are researched vs. design-doc-default, (d) the operator-driven refresh contract.

### 6\. Tests

`tests/execution/regt_margin_attribution/test_config.py`:

* `test_config_round_trips_minimal_yaml` — write a minimal valid yaml to a temp file, load it, assert all fields equal expected values.
* `test_config_rejects_non_finite_risk_free_rate` — yaml with `risk_free_rate_annual: .inf` raises validation error.
* `test_config_rejects_invalid_iv_shock_direction` — `worst_down_multiplier: 0.9` raises validation error.
* `test_config_rejects_shock_outside_unit_interval` — `high_cap_equity: 1.5` raises validation error.
* `test_config_normalises_per_symbol_override_keys_to_uppercase` — yaml `spy: 0.15` loads with key `SPY`.
* `test_default_path_yaml_loads_cleanly` — `load_regt_margin_attribution_config()` returns a model with `pm_model_version == "ibkr_mirror_v1_2026Q2"` and at least the minimum coverage list of ETFs in `per_symbol_overrides`.

### Out of scope

* Reg T per-leg percentages (long equity 50%, short equity 150%, long options 100%, short options FINRA formula) — these are definitional named constants in story 03a.
* Shock-grid size — named constant in story 03b.
* The actual stress / margin computations — stories 03a, 03b, 04, 05.

## Acceptance criteria

- [ ] `src/alphamind/execution/regt_margin_attribution/__init__.py` exports `RegTMarginAttributionConfig` and `load_regt_margin_attribution_config`.
- [ ] `RegTMarginAttributionConfig`, `ShockParameters`, `IvShockMultipliers` are frozen Pydantic models defined at `src/alphamind/execution/regt_margin_attribution/config.py`.
- [ ] `config/regt_margin_attribution.yaml` exists; `load_regt_margin_attribution_config()` (no path argument) returns a model with `pm_model_version == "ibkr_mirror_v1_2026Q2"`.
- [ ] The yaml contains at least the major-ETF + sector-SPDR symbols in `per_symbol_overrides` (SPY, QQQ, IWM, DIA, VTI, VOO, XLF, XLE, XLK, XLV, XLY, XLP, XLU, XLI, XLB, XLRE, XLC) plus at least 5 of the top S&P 500 symbols listed.
- [ ] The yaml's leading comment block names the IBKR disclosure URL + access date, or explicitly states "design-doc defaults applied — IBKR research deferred" if the page was unreachable.
- [ ] Validators reject: non-finite `risk_free_rate_annual`; `worst_down_multiplier < 1.0`; `worst_up_multiplier > 1.0`; any shock percentage outside `(0.0, 1.0)`.
- [ ] Per-symbol override keys are normalised to upper-case on construction.
- [ ] `tests/execution/regt_margin_attribution/test_config.py` covers the six tests named in Scope §6 and passes under `uv run pytest tests/execution/regt_margin_attribution/ -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` are clean.

## Verification

Run `uv run pytest tests/execution/regt_margin_attribution/test_config.py -n auto`. Inspect `config/regt_margin_attribution.yaml` to confirm the leading comment block names the IBKR disclosure source (or the design-doc-default fallback notice) and that `per_symbol_overrides` contains the minimum coverage list.