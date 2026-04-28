---
status: not_started
completed_date:
commit_id:
---

# 02 — Package skeleton & configuration scaffolding

## Goal

Stand up the Python package layout for the Portfolio state work tree under `src/alphamind/portfolio_state/`, plus the config file the snapshot assembler reads. Empty packages with import-only smoke test, plus a parseable YAML stub. All subsequent stories drop modules into this skeleton.

## Reading

- `docs/design/01-data-layer/internal/portfolio-state.md` — the full feature spec; the six categories the package serves
- `docs/design/01-data-layer/internal/README.md` — the consumer / cadence map informing module boundaries
- `docs/design/05-execution-layer/state-persistence.md` § Read paths § Invocation snapshot — the snapshot read contract this work tree implements on the data-layer side
- `docs/implementation/01-data-layer/collector/02-project-skeleton.md` — sibling pattern (top-level `src/alphamind/<feature>/` + `tests/<feature>/`)
- `docs/implementation/03-analysis-layer/synthesizer/02-agent-configuration-scaffolding.md` — sibling pattern for `config/<feature>.yaml`
- `pyproject.toml` — declares the package layout (`src/alphamind/`)

## Depends on

- 01

## Scope

In scope:

- Top-level package at `src/alphamind/portfolio_state/` with submodules organized by concern. Empty `__init__.py` in every directory. The structure subsequent stories populate:

  ```
  src/alphamind/portfolio_state/
      __init__.py
      records/
          __init__.py
          positions.py          # 03a — base + equity/options/strategy
          theses.py             # 03b — thesis records, components, status, resolutions
          orders.py             # 03c — order + bracket records (one file; tightly coupled)
          capital.py            # 03d — cash ledger, risk budget, active risk params, drawdown
          activity_log.py       # 03e — discriminated union over event-type catalog
          thesis_quality.py     # 03f — trailing aggregates
      snapshot.py               # 04a — master PortfolioStateSnapshot
      repository.py             # 04b — PortfolioStateRepository Protocol + stub
      pricing.py                # 03g — CurrentPriceProvider Protocol + stub
      computations/
          __init__.py
          positions.py          # 05a — market value, weight, age, distance, R:R
          pnl.py                # 05b — portfolio-level P/L + drawdown rollups
          exposure.py           # 05c — sector / directional / gross rollups
          risk_budget.py        # 05d — per-rule consumption
          activity_log.py       # 05e — 5a/5b/5c filtering
      assembler.py              # 06 — SnapshotAssembler
      consumers/
          __init__.py
          synthesizer.py        # 07 — view projection + reader protocol + stub
          analyst.py            # 07
          strategist.py         # 07
          portfolio_manager.py  # 07
      freshness.py              # 08 — invocation-snapshot semantics, staleness reporting
  ```

- Test mirror at `tests/portfolio_state/` with `__init__.py` only (subsequent stories populate per-module test files).

- `config/portfolio_state.yaml` with the keys the snapshot assembler consumes:

  ```yaml
  portfolio_state:
    pm_decision_log:
      sliding_window_invocations: 3        # category 5b — recent N invocations
    thesis_resolutions:
      lookback_trading_days: 20            # category 3c — rolling resolution window
    thesis_quality_aggregates:
      trailing_windows_days: [5, 20]       # category 6 — windows for aggregate computation
    snapshot_freshness:
      max_phase1_to_snapshot_seconds: 30   # see freshness.md (story 08); above this, log warning
      max_price_age_seconds: 900           # current price freshness threshold; above this, set staleness flag on affected positions
  ```

  Field rationale:
    - `sliding_window_invocations: 3` — design says "configurable, typically 2–3"; pick the upper bound so PMs see one extra invocation of recent decisions. Operator tunable.
    - `lookback_trading_days: 20` — design says "e.g., last 10 or 5 trading days"; 20 covers the longer window the strategist references for thesis-status calibration without paging in inception-window data.
    - `trailing_windows_days: [5, 20]` — matches the design's 5d/20d/inception ladder; inception is computed dynamically (no fixed day count).
    - `max_phase1_to_snapshot_seconds` and `max_price_age_seconds` — staleness thresholds the assembler enforces. The repository layer reports `phase1_committed_at`; the price provider reports `as_of`. Story 08 uses these.

- Pydantic v2 `PortfolioStateConfig` model in `src/alphamind/portfolio_state/__init__.py` (or a dedicated `config.py` module — choose one and document; subsequent stories must import the same path) with:
  - `pm_decision_log_sliding_window_invocations: int` (positive)
  - `thesis_resolutions_lookback_trading_days: int` (positive)
  - `thesis_quality_aggregates_trailing_windows_days: tuple[int, ...]` (non-empty, all positive)
  - `snapshot_freshness_max_phase1_to_snapshot_seconds: float` (positive)
  - `snapshot_freshness_max_price_age_seconds: float` (positive)
  - `model_config = {"frozen": True}`
  - A `load_portfolio_state_config(path: pathlib.Path) -> PortfolioStateConfig` helper that parses the YAML and validates against the model.

- `tests/portfolio_state/test_config.py`:
  - `config/portfolio_state.yaml` parses into a `PortfolioStateConfig` with the documented values.
  - Negative or zero values for any positive field fail parse with a field-path-bearing error.
  - Empty `trailing_windows_days` list fails parse.

- `tests/portfolio_state/test_imports.py`:
  - Imports `alphamind.portfolio_state` and each subpackage (`records`, `computations`, `consumers`); each import succeeds.

Out of scope:
- Any record class definitions (stories 03a–f).
- The repository, snapshot, assembler, computations, consumer projections (stories 04+).
- Wiring `PortfolioStateConfig` into the broader cascaded config (`main.yaml` profile / regime / overlay composition) — this story lands a flat YAML; composition is a future cross-feature concern.

## Notes

`src/alphamind/portfolio_state/` is a top-level package alongside `analysis/`, `decision/`, `execution/`, `persistence/`. The Portfolio state feature is a data-layer concern but `data_layer/` does not exist as a Python package today; mirroring the design's directory naming (`docs/design/01-data-layer/internal/portfolio-state.md`) into a top-level Python package is the simplest mapping. If a `data_layer/` umbrella package emerges later (e.g., when the collector consolidates), this package can move with a single rename.

Per-component subdirectory depth (`records/positions.py`, `computations/exposure.py`) over per-layer depth (`positions/records.py`, `positions/computations.py`) is the convention the codebase already follows for the synthesizer and decision-layer scaffolding (see `feedback_scaffold_per_component_depth.md`).

The configuration file lives at `config/portfolio_state.yaml` rather than under a `data_layer/` subdir because the existing `config/` directory is flat (`assets.yaml`, `data_sources.yaml`, etc.). When the broader config composition lands, this file can be moved or re-namespaced without touching the loader contract.

Per `feedback_avoid_numeric_anchors.md`, the YAML's numeric defaults are operator-tunable and rationalized in this story's notes — they are not a target the LLM should treat as authoritative thresholds. Subsequent stories that compute against them must not introduce hardcoded magic numbers.

The `consumers/` subdirectory is where per-consumer view projections live. The synthesizer already has its own slim `PortfolioStateReader` Protocol at `src/alphamind/analysis/synthesizer/portfolio_state.py` (story 05b in the synthesizer work tree). Story 07 here lands the canonical types and reader protocols; the synthesizer's local Protocol can converge against them in a follow-up — that convergence is not part of this work tree.

## Acceptance criteria

- [ ] `src/alphamind/portfolio_state/__init__.py` exists.
- [ ] Every subdirectory listed in scope (`records/`, `computations/`, `consumers/`) has an `__init__.py`.
- [ ] `tests/portfolio_state/__init__.py` exists.
- [ ] `config/portfolio_state.yaml` exists with the documented top-level keys.
- [ ] `PortfolioStateConfig` Pydantic model exists at the path declared in this story; subsequent stories import it from that path.
- [ ] `load_portfolio_state_config(path)` parses the YAML into a `PortfolioStateConfig` whose field values match the documented defaults.
- [ ] Negative or zero values for any positive field fail parse with a field-path-bearing error.
- [ ] Empty `trailing_windows_days` fails parse.
- [ ] `tests/portfolio_state/test_imports.py` imports `alphamind.portfolio_state` and each subpackage successfully.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
