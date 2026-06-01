# 02 — Class-group composition

## Goal

Introduce the `ClassGroup` typed value object and the pure function `compose_class_groups(positions)` that partitions a tuple of `PositionRecord` into class groups — one underlying ticker plus all equity, option, and strategy positions written on it. The IBKR-mirror v1 stress in story 03b consumes class groups as the unit of stress aggregation; the Reg T per-leg formula in story 03a iterates positions independently, so this function exists primarily for the PM-equivalent path. Defines the only new typed record this work tree introduces.

## Reading

* `docs/design/05-execution-layer/regt-margin-attribution.md` § Portfolio-margin reference model — defines class group as "One underlying plus all derivatives written on it. An equity-only position is its own class group. An options strategy on NVDA contributes to the NVDA class group along with any long or short NVDA stock."
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord`, the `EquityPositionDetails` / `OptionsPositionDetails` / `StrategyPositionDetails` discriminated variants, `StrategyLeg` shape; identify the field carrying the underlying symbol on each variant.
* `docs/design/05-execution-layer/position-model.md` § Strategy position — confirms a strategy position has a single underlying (multi-leg strategies are single-name; legs hedge each other).
* `src/alphamind/execution/regt_margin_attribution/config.py` (from story 01a) — `RegTMarginAttributionConfig` is consumed by downstream stories; not needed here, but the package layout informs file placement.
* `ALP-126` parent issue § Architectural invariants — purity rule (no I/O, no clock reads), no-invented-component-names rule.

## Depends on

* `ALP-421` (01a — Package skeleton + IBKR-mirror v1 config) — package must exist for the new module to live in.

## Scope

In scope: `src/alphamind/execution/regt_margin_attribution/class_groups.py` and `tests/execution/regt_margin_attribution/test_class_groups.py`.

### 1\. `ClassGroup` typed record

```python
@dataclass(frozen=True, slots=True)
class ClassGroup:
    """One underlying symbol plus every position written on it.

    Mirrors ``regt-margin-attribution.md § Portfolio-margin reference model``:
    a class group's PM-equivalent margin is the worst-case P/L magnitude
    across the 10-point shock grid applied to all instruments in the group.
    """

    underlying_symbol: str
    positions: tuple[PositionRecord, ...]
```

* `underlying_symbol` is normalised to upper-case at construction time (validator in `__post_init__`).
* `positions` is non-empty (validator); a class group with no positions is meaningless.
* Frozen and slotted to match the codebase convention for value objects.

### 2\. `compose_class_groups` function

```python
def compose_class_groups(
    positions: tuple[PositionRecord, ...],
) -> tuple[ClassGroup, ...]:
    """Partition open positions into class groups keyed on underlying symbol.

    Pure function. Returns a tuple sorted by ``underlying_symbol`` for
    deterministic downstream iteration. An empty input returns an empty
    tuple.

    Equity positions contribute their ticker as the underlying. Options
    positions contribute the option's underlying symbol (from
    ``OptionsPositionDetails``). Strategy positions contribute the
    strategy's single underlying symbol (from ``StrategyPositionDetails``;
    all legs share an underlying per the position-model invariant).
    """
```

Implementation extracts the underlying symbol per variant via a small dispatch helper (`_underlying_of(position)`) that pattern-matches the position-details union and returns the appropriate field. Symbols are upper-cased before grouping so that case-inconsistent inputs converge.

### 3\. Tests

`tests/execution/regt_margin_attribution/test_class_groups.py`:

* `test_empty_positions_returns_empty_class_groups` — `compose_class_groups(())` returns `()`.
* `test_single_equity_position_forms_own_class_group` — one long AAPL equity → one ClassGroup with `underlying_symbol="AAPL"` and that one position.
* `test_two_equity_positions_same_underlying_form_one_class_group` — long AAPL + short AAPL (hedged book) → one ClassGroup with both positions.
* `test_two_equity_positions_different_underlyings_form_two_class_groups` — AAPL + NVDA → two ClassGroups, sorted alphabetically.
* `test_equity_plus_option_on_same_underlying_form_one_class_group` — long AAPL stock + AAPL call → one ClassGroup, both positions.
* `test_strategy_position_groups_with_its_underlying` — NVDA iron condor → ClassGroup with `underlying_symbol="NVDA"` containing the strategy position. If long NVDA stock is also present, they share the class group.
* `test_class_groups_sorted_by_underlying_symbol_ascending` — input order (NVDA, AAPL, MSFT) → output order (AAPL, MSFT, NVDA).
* `test_case_inconsistent_underlying_symbols_converge` — equity with `symbol="aapl"` and option with underlying `"AAPL"` → one ClassGroup with `underlying_symbol="AAPL"`.
* `test_class_group_rejects_empty_positions` — `ClassGroup(underlying_symbol="AAPL", positions=())` raises `ValueError` at construction.
* `test_class_group_normalises_underlying_to_uppercase` — `ClassGroup(underlying_symbol="aapl", positions=(...,))` exposes `underlying_symbol == "AAPL"`.

Use a small set of `PositionRecord` fixtures constructed inline (no shared fixture module — keep tests self-contained per the codebase pattern).

### Out of scope

* Reg T per-leg margin computation — story 03a iterates positions directly without going through class groups.
* PM stress computation — story 03b consumes `ClassGroup` instances as input.
* Caching or memoisation — `compose_class_groups` is called once per attribution invocation in story 05; no memoisation needed.

## Acceptance criteria

- [ ] `ClassGroup` is defined in `src/alphamind/execution/regt_margin_attribution/class_groups.py` as a frozen, slotted dataclass with `underlying_symbol: str` and `positions: tuple[PositionRecord, ...]`.
- [ ] `ClassGroup` rejects empty `positions` at construction.
- [ ] `ClassGroup` normalises `underlying_symbol` to upper-case at construction.
- [ ] `compose_class_groups` returns class groups sorted by `underlying_symbol` ascending.
- [ ] `compose_class_groups` returns an empty tuple on empty input.
- [ ] An equity position, an option on the same underlying, and a strategy on the same underlying all land in one `ClassGroup`.
- [ ] Two positions with different underlyings land in two different `ClassGroup` instances.
- [ ] Case-inconsistent underlying symbols converge to the upper-case key.
- [ ] `compose_class_groups` is a pure function (no I/O, no clock reads, no global mutation).
- [ ] `ClassGroup` and `compose_class_groups` are exposed via `src/alphamind/execution/regt_margin_attribution/__init__.py`'s `__all__`.
- [ ] `tests/execution/regt_margin_attribution/test_class_groups.py` covers the ten tests named in Scope §3 and passes under `uv run pytest tests/execution/regt_margin_attribution/ -n auto`.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy` are clean.

## Verification

Run `uv run pytest tests/execution/regt_margin_attribution/test_class_groups.py -n auto`. Spot-check `class_groups.py` to confirm: (a) the per-variant dispatch helper handles `EquityPositionDetails`, `OptionsPositionDetails`, `StrategyPositionDetails`; (b) the function reads no module-level state; (c) results are sorted deterministically.