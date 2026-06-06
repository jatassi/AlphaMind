# 01 — Package skeleton + fee schedule + per-fill fee computer

## Goal

Lands the foundation for the paper-evaluation harness: replaces the placeholder `paper_evaluation_harness/__init__.py` with a real package skeleton, extends `ExecutionConfig.paper_harness` with a `fee_schedule` block sourced from Alpaca's published regulatory taxonomy, and ships a pure `compute_regulatory_fees(...)` function that returns the per-fill fee Money estimate for equity and options fills. Downstream stories (02a/02b/03) layer on top of this surface.

## Reading

* `docs/design/05-execution-layer/paper-evaluation-harness.md` § Regulatory fees — fee taxonomy by instrument class and side
* `docs/design/05-execution-layer/broker-adapter.md` § Fee reporting — current TAF/CAT/SEC/ORF/OCC rates and which side they apply to
* `src/alphamind/config/models/execution.py` — existing `PaperHarness` + `ExecutionConfig` Pydantic models to extend
* `config/execution.yaml` — existing `paper_harness:` block to extend with `fee_schedule:`
* `src/alphamind/_kernel/money.py` — `Money`, `money()` boundary helper used at the typed-record edge
* `src/alphamind/portfolio_state/records/positions.py` § `InstrumentType` — the EQUITY/OPTIONS/STRATEGY enum the fee dispatch keys on
* `src/alphamind/execution/paper_evaluation_harness/__init__.py` — the current placeholder to replace

## Depends on

None. First story in the work tree.

## Scope

In scope, all under `src/alphamind/execution/paper_evaluation_harness/` and `src/alphamind/config/`. Tests at `tests/execution/paper_evaluation_harness/`.

### 1\. Pydantic `FeeSchedule` model

Add a frozen `FeeSchedule` Pydantic model in `src/alphamind/config/models/execution.py` carrying the regulatory fee rates as typed fields. The exact field shape depends on each fee's unit — confirm against `broker-adapter.md § Fee reporting`:

* `cat_per_executed_share: float` (CAT applies to all executed shares; per-share rate)
* `taf_per_share_sells: float` (TAF on equity sells; per-share rate with documented cap if applicable)
* `sec_pct_of_notional_sells: float` (SEC Section 31 fee on equity and options sells; rate per dollar of notional)
* `orf_per_options_contract: float` (ORF on all options executions; per-contract rate)
* `occ_per_options_contract: float` (OCC clearing fee on all options executions; per-contract rate)

All rates `>= 0`. Add field validators to enforce non-negativity. Add `FeeSchedule` as a typed field on the existing `PaperHarness` model.

### 2\. `config/execution.yaml` extension

Add a `fee_schedule:` block under `paper_harness:` populated with current FINRA/SEC/OCC published rates. Cite the source URL inline as a YAML comment so a future updater knows where to look. The current paper-harness block expands from:

```yaml
paper_harness:
  spread_buffer_pct: 10
  impact_coefficients:
    market: 0.5
    limit: 0.25
    stop: 0.75
```

to add:

```yaml
paper_harness:
  spread_buffer_pct: 10
  impact_coefficients:
    market: 0.5
    limit: 0.25
    stop: 0.75
  fee_schedule:
    # rates sourced from <FINRA/SEC/OCC docs; cite URL>
    cat_per_executed_share: <value>
    taf_per_share_sells: <value>
    sec_pct_of_notional_sells: <value>
    orf_per_options_contract: <value>
    occ_per_options_contract: <value>
```

The subagent looks up the current published rates and pins values. If a rate cannot be found from an authoritative source, write the YAML value as `0.0` with a comment explaining the gap, AND surface to the orchestrator (do not invent a value).

### 3\. `compute_regulatory_fees` function

New module `src/alphamind/execution/paper_evaluation_harness/fees.py` with:

```python
def compute_regulatory_fees(
    *,
    instrument_type: InstrumentType,
    side: Literal["buy", "sell"],
    fill_quantity: float,
    fill_price: Price,
    schedule: FeeSchedule,
) -> Money: ...
```

Dispatch by `instrument_type`:

* **EQUITY:** CAT applies to all fills (per-share); TAF applies to sells only (per-share); SEC applies to sells only (per-dollar of notional = `fill_price * fill_quantity`).
* **OPTIONS:** CAT applies to all fills (per-share treated as per-contract \* 100? consult `broker-adapter.md § Fee reporting` for the convention — if Alpaca's CAT applies per-contract for options, schema MUST distinguish; if per-share at the equivalent-share level, the schema can collapse. Subagent reads the doc and pins the chosen convention in the docstring); SEC applies to options sells only (per-dollar of notional with options notional being `fill_price * fill_quantity * 100` for standard contracts); ORF applies to all options fills (per-contract); OCC applies to all options fills (per-contract).
* **STRATEGY:** unreachable at this layer — strategy fills enter the wedge as their per-leg `InstrumentType` (EQUITY or OPTIONS) per parent decision (G). Raise `NotImplementedError` if called with `STRATEGY` to fail loud if the wedge changes.

The function is pure (no DB / HTTP / config-loading). Return is always non-negative Money.

### 4\. Package skeleton

Replace `src/alphamind/execution/paper_evaluation_harness/__init__.py` with proper re-exports:

```python
"""Paper-evaluation harness — calibrates Alpaca paper fills with estimated live-execution drag."""

from alphamind.execution.paper_evaluation_harness.fees import compute_regulatory_fees

__all__ = ["compute_regulatory_fees"]
```

Subsequent stories extend `__all__`.

### Out of scope

* Spread estimator (story 02a)
* Impact estimator (story 02b)
* Composition into `LiveExecutionEstimate` (story 03)
* Wedge integration (story 04a)
* P/L aggregate (story 04b)

## Acceptance criteria

- [ ] `FeeSchedule` is importable from `alphamind.config.models.execution` and is frozen.
- [ ] `config/execution.yaml § paper_harness.fee_schedule` exists with five named rate keys, each pinned to a current published value or `0.0` + surfacing comment.
- [ ] Loading the config tree via the existing `alphamind.config.load` path succeeds with the new block; no other yaml or model needs updating.
- [ ] `compute_regulatory_fees(instrument_type=InstrumentType.EQUITY, side="buy", fill_quantity=100, fill_price=price("50"), schedule=...)` returns CAT-only (no TAF, no SEC).
- [ ] `compute_regulatory_fees(instrument_type=InstrumentType.EQUITY, side="sell", fill_quantity=100, fill_price=price("50"), schedule=...)` returns CAT + TAF + SEC summed.
- [ ] `compute_regulatory_fees(instrument_type=InstrumentType.OPTIONS, side="buy", fill_quantity=1, fill_price=price("2.50"), schedule=...)` returns CAT + ORF + OCC (no SEC for buy side).
- [ ] `compute_regulatory_fees(instrument_type=InstrumentType.OPTIONS, side="sell", fill_quantity=1, fill_price=price("2.50"), schedule=...)` returns CAT + SEC + ORF + OCC.
- [ ] `compute_regulatory_fees(instrument_type=InstrumentType.STRATEGY, ...)` raises `NotImplementedError`.
- [ ] All returned values are non-negative `Money`.
- [ ] `paper_evaluation_harness/__init__.py` re-exports `compute_regulatory_fees`.
- [ ] `tests/execution/paper_evaluation_harness/test_fees.py` exists and passes under `uv run pytest tests/execution/paper_evaluation_harness/test_fees.py --testmon -n auto`.
- [ ] `uv run pytest --testmon -n auto` (full suite — testmon-skipped where unaffected) passes.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all clean.

## Verification

Run the scoped test file plus the lint chain. Spot-check `config/execution.yaml` renders correctly via `python -c "from alphamind.config.load import load_config_tree; ..."` (or whatever the existing config-load entry point is).