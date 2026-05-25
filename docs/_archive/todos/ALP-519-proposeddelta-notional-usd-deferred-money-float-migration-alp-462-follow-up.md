## Symptom

`src/alphamind/decision/proposal_pre_processor/translator.py:200-214`:

```python
def _notional_for_recommendation(recommendation: Recommendation) -> float:
    """Resolve notional_usd from recommendation position_size.

    For options/strategy: use premium_at_risk when set, else dollar_value.
    For equity: use dollar_value.

    ALP-462 — ``position_size.dollar_value`` / ``premium_at_risk`` are
    :class:`Money` (Decimal-backed) on the analyst boundary. Cast to ``float``
    here at the proposal-pre-processor boundary; ``ProposedDelta.notional_usd``
    is still float (a deferred migration outside the ALP-462 file list).
    """
    asset_type = recommendation.instrument.asset_type
    ps = recommendation.position_size
    if asset_type in ("option", "strategy") and ps.premium_at_risk is not None:
        return float(ps.premium_at_risk)
    return float(ps.dollar_value)
```

## Production impact

Minor. The cast is at a single boundary; downstream consumers (guardrail evaluation, risk projection) consume float. The only practical issue is the inconsistency between the `Money` ingress and float internal type — every reader has to remember which type they're dealing with.

## Scope

**(A) Migrate** `ProposedDelta.notional_usd` **to** `Money`**.** Update `decision/proposal_pre_processor/models.py` (or wherever `ProposedDelta` lives).

**(B) Propagate.** Update all consumers in `decision/proposal_pre_processor/`, `risk_guardrails/`, and `decision/portfolio_manager/` to handle `Money` instead of `float`. Boundary cast (if needed for guardrail evaluation's float surfaces) moves outward to those consumers.

**(C) Remove the boundary cast.** `_notional_for_recommendation` and `_notional_and_quantity_for_assessment` return `Money` not `float`.

## Acceptance criteria

- [ ] `ProposedDelta.notional_usd` typed as `Money`.
- [ ] All consumers updated.
- [ ] `_notional_for_recommendation` returns `Money`; no `float(...)` cast.
- [ ] `uv run pytest -n auto` passes; `uv run mypy` passes.

## Verification

* `grep -n "notional_usd" src/alphamind/decision/` shows consistent `Money` typing.
* `grep -n "float(ps.dollar_value)\|float(ps.premium_at_risk)" src/alphamind/decision/proposal_pre_processor/translator.py` returns zero hits.

## Priority

Low — typed-money cleanup, no behavioral issue.