# Position-level constraint proximity uses magnitude comparison — positive P/L greater than max-loss magnitude flagged CRITICAL

## Symptom

The position-level constraint-proximity check in the strategist/PM guardrail header compares the absolute value of a position's P/L against the absolute value of its `max_loss` cap, rather than checking whether the P/L is *below* the cap. A position with a large positive (gain) P/L therefore gets flagged as `[🔴 CRITICAL]` — the same flag used for a position near its max-loss breach.

In this invocation, `debug-pos-07` (MSFT strategy) has `P/L: +$4,895 since open (+19900.0%)` against a `max_loss: -80.0%` cap. The +19900% gain is the opposite of an 80% loss, but the magnitude check `|19900| > |80|` fires CRITICAL.

The strategist correctly recognized the +19900% reading as anomalous (held pending RECONCILIATION_ALERT resolution rather than acting on phantom P/L), so this didn't propagate to a wrong trade decision in this invocation. But the underlying check logic is wrong — in production, a real position that just rallied beyond its max-loss-magnitude is the *opposite* of a risk event and should not trigger a CRITICAL flag.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`decision/portfolio_manager/user_message.md` line 32:

```
Position-level constraint proximity:
  debug-pos-00: 16.9% of portfolio (max 5.0%) — P/L: +0.0% of cost (max loss: -30.0%) [BLOCKED]
  debug-pos-01: 17.3% of portfolio (max 5.0%) — P/L: +0.0% of cost (max loss: -30.0%) [BLOCKED]
  debug-pos-02: 12.7% of portfolio (max 5.0%) — P/L: +0.0% of cost (max loss: -30.0%) [BLOCKED]
  debug-pos-03: 13.6% of portfolio (max 5.0%) — P/L: +0.0% of cost (max loss: -30.0%) [BLOCKED]
  debug-pos-04:  6.8% of portfolio (max 5.0%) — P/L: +0.0% of cost (max loss: -30.0%) [BLOCKED]
  debug-pos-05:  4.0% of portfolio (max 5.0%) — P/L: +0.0% of cost (max loss: -80.0%) [⚠ WARNING]
  debug-pos-06:  2.0% of portfolio (max 5.0%) — P/L: +0.0% of cost (max loss: -80.0%)
  debug-pos-07:  4.5% of portfolio (max 5.0%) — P/L: +19900.0% of cost (max loss: -80.0%) [🔴 CRITICAL]
```

debug-pos-07 sits at 4.5% of portfolio (under the 5.0% size cap, so no BLOCKED flag from sizing) and P/L is +19900% (very positive). The only reason for `[🔴 CRITICAL]` is the max-loss check, which is being applied via magnitude rather than sign.

## Root cause

The check compares `|P/L|` against `|max_loss|` rather than checking if `P/L >= max_loss` (where both are signed percentages of cost). For losses (P/L negative), the magnitude check coincidentally produces the right answer because `|loss| > |max_loss|` iff `loss < max_loss`. For gains (P/L positive), the magnitude check incorrectly fires when `gain > |max_loss|`.

The fix is to use signed comparison: a position is `CRITICAL` only when `P/L <= max_loss` (where max_loss is a negative number) — i.e., when the position is *at or beyond* its loss limit.

## Scope

In the position-level constraint-proximity computation:

1. Replace magnitude comparison with signed comparison: `CRITICAL` fires only when P/L (signed) is at or below the `max_loss` floor (signed).
2. Audit other similar constraint checks (e.g., `WARNING` zone) for the same magnitude-vs-sign issue.
3. Add a unit test for a position with P/L exceeding the magnitude of its max_loss in the positive direction — confirm no CRITICAL flag fires.

## Acceptance criteria

- [ ] A position with P/L = +19900% and max_loss = -80% does NOT receive a `[🔴 CRITICAL]` flag.
- [ ] A position with P/L = -85% and max_loss = -80% DOES receive a `[🔴 CRITICAL]` flag (existing behavior preserved).
- [ ] All severity-zone checks (NORMAL, WARNING, CRITICAL, BLOCKED) use signed comparison consistent with the sign convention of the underlying field.

## Verification

* Unit test: position with P/L = +19900%, max_loss = -80% → no CRITICAL flag.
* Unit test: position with P/L = -85%, max_loss = -80% → CRITICAL flag.
* Re-run debug-e2e (with the synthetic +19900% MSFT seed preserved); confirm debug-pos-07 no longer carries CRITICAL.

## Notes

The +19900% P/L on debug-pos-07 is a synthetic seed artifact (the position has $25 cost and $4,920 mark, both fixed by the test harness). The bug surfaced because the strategist correctly questioned the anomalous reading — but the constraint-check logic itself is genuinely incorrect and would produce wrong CRITICAL flags on real winning positions in production. Medium priority because the strategist's anti-pattern discipline (refused to act on unreconciled P/L) is a backstop, but the bug should still be fixed.
