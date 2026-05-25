## Symptom

Latent — not yet observed at runtime because the upstream `KeyError` in [ALP-505](https://linear.app/alphamind-jatassi/issue/ALP-505/scheduler-builds-libraryconfig-with-empty-escalation-zones-and-zero) currently aborts the pipeline before any CANCEL contribution is evaluated. Once [ALP-505](https://linear.app/alphamind-jatassi/issue/ALP-505/scheduler-builds-libraryconfig-with-empty-escalation-zones-and-zero) is fixed and a CANCEL on a position with reserved capital reaches `_pending_order_capital_contribute`, the contribution will silently report `0.0` instead of releasing the reserved capital.

Same incomplete-implementation shape as [ALP-504](https://linear.app/alphamind-jatassi/issue/ALP-504/close-on-optionsstrategy-rule-contributions-read-empty-proposal-dae): a contribution function that depends on an existing-position-side field, with the field zeroed out at the translator boundary.

## Root cause

`src/alphamind/risk_guardrails/library_snapshot.py:248-261` stamps every `ExistingPosition`:

```python
existing_positions[pos.position_id] = ExistingPosition(
    ...
    # reserves_capital_usd: 0.0 — PositionRecord does not carry this field
    reserves_capital_usd=0.0,
    ...
)
```

`_pending_order_capital_contribute` (`rules/capital.py:81-100`) reads this field on `Action.CANCEL`:

```python
if proposal.action is Action.CANCEL:
    existing = existing_position(proposal, state)
    if existing is None:
        return 0.0
    return -existing.reserves_capital_usd / state.portfolio_value_usd * 100.0
```

With the field permanently 0, every CANCEL-on-non-marketable-limit contribution returns `0.0`. The library math is correct — `tests/risk_guardrails/guardrail_evaluation/test_rules_capital.py:243-260` constructs `ExistingPosition` with `reserves_capital_usd=3_000.0` and asserts the expected release magnitude. The production translator drops the data.

`PositionRecord` does not carry the per-position field. The portfolio-level aggregate `reserved_capital_usd` exists at `portfolio_state/records/cash.py:48` (cash-ledger total), but there's no per-position breakdown threaded through to `to_library_snapshot`.

## Why it surfaced now

Discovered in the post-[ALP-504](https://linear.app/alphamind-jatassi/issue/ALP-504/close-on-optionsstrategy-rule-contributions-read-empty-proposal-dae) audit for similar incomplete-implementation patterns. Latent until [ALP-505](https://linear.app/alphamind-jatassi/issue/ALP-505/scheduler-builds-libraryconfig-with-empty-escalation-zones-and-zero) is fixed AND a CANCEL with non-zero reserved capital reaches the pre-processor.

## Why tests don't catch it

Library-layer tests construct `ExistingPosition` directly with `reserves_capital_usd=3_000.0` and pass. No integration test runs `to_library_snapshot` against a snapshot containing non-marketable limit orders and asserts the resulting `ExistingPosition.reserves_capital_usd`.

## Scope — two options

**(A) Add a per-position** `reserves_capital_usd` **field to** `PositionRecord` and populate it at portfolio_state assembly time from the pending-orders ledger. Cleaner if `PositionRecord` is the canonical position-level read.

**(B) Pass the pending-orders ledger** (or a `Mapping[position_id, reserved_capital]`) as a parameter to `to_library_snapshot`, similar to `sector_resolver` and `borrow_cost_resolver` today. Less invasive if the pending-orders ledger lives elsewhere.

## Acceptance criteria

* `to_library_snapshot` populates `ExistingPosition.reserves_capital_usd` for positions backing non-marketable pending limit orders.
* New integration test asserts a CANCEL on such a position contributes the expected release magnitude to `pending_order_capital_pct`.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.

## Related

* Related: [ALP-504](https://linear.app/alphamind-jatassi/issue/ALP-504/close-on-optionsstrategy-rule-contributions-read-empty-proposal-dae) (same incomplete-implementation shape).
* Blocked by: [ALP-505](https://linear.app/alphamind-jatassi/issue/ALP-505/scheduler-builds-libraryconfig-with-empty-escalation-zones-and-zero) — until A is fixed, the CANCEL contribution path can't be exercised end-to-end.

## Reading

* `src/alphamind/risk_guardrails/library_snapshot.py:230-280` — translator's `ExistingPosition` construction.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py:181-206` — `ExistingPosition.reserves_capital_usd: float`.
* `src/alphamind/risk_guardrails/guardrail_evaluation/rules/capital.py:81-100` — `_pending_order_capital_contribute`.
* `src/alphamind/portfolio_state/records/cash.py:48` — portfolio-level `reserved_capital_usd` (cash-ledger total, no per-position breakdown).
* `tests/risk_guardrails/guardrail_evaluation/test_rules_capital.py:243-260` — library-layer test that passes with non-zero reserved capital.