# debug-pos-07 P/L inconsistency — +19900% / +$4,895 on MSFT against +0% / $0 on every other position, surfaces unreconciled Alpaca/local state

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the strategist's input bundle shows 8 held positions all placed at 2026-05-19T03:06:54Z (the invocation start) with Age=0.0 hours. The position-level P/L values are:

```
debug-pos-00 (NVDA, long, 16.9%):  P/L: +$0 since open (+0.0%)
debug-pos-01 (JPM, long, 17.3%):   P/L: +$0 since open (+0.0%)
debug-pos-02 (GOOGL, long, 12.7%): P/L: +$0 since open (+0.0%)
debug-pos-03 (COP, long, 13.6%):   P/L: +$0 since open (+0.0%)
debug-pos-04 (TSLA, short, 6.8%):  P/L: +$0 since open (+0.0%)
debug-pos-05 (AAPL, option, 4.0%): P/L: +$0 since open (+0.0%)
debug-pos-06 (META, option, 2.0%): P/L: +$0 since open (+0.0%)
debug-pos-07 (MSFT, strategy, 4.5%): P/L: +$4,895 since open (+19900.0%)
```

7 of 8 positions show P/L = $0 / 0% (consistent with the synthetic seed of "just placed" positions). debug-pos-07 alone shows +$4,895 / +19900%. The strategist identified the inconsistency:

> "The CRITICAL flag in the header should be reconciled against the inconsistent P/L display; the reconciliation alert in the activity log (Alpaca holds MSFT qty=1.0; no matching local position) is the most likely explanation for the display anomaly and warrants engine-side investigation rather than a thesis-driven action."

The Activity Log explicitly carries the reconciliation alert:

```
[2026-05-19T03:06:56Z] RECONCILIATION_ALERT: position alpaca_only_position mismatch: local=0.0 vs alpaca=1.0 — Alpaca holds MSFT qty=1.0; no matching local position
```

So Alpaca holds 1 share of MSFT that the local DB doesn't know about. The +19900% P/L on debug-pos-07 (the MSFT strategy position) appears to be reading the Alpaca-side MSFT holding's market value as P/L for the local strategy position. The cost basis on the local strategy is $25 (from the synthetic seed) and the Alpaca-side MSFT share at current market price is \~$4,895, producing the anomalous +$4,895 P/L on a $25 cost basis = +19580% ≈ +19900%.

This is a real engine-side reconciliation bug — the P/L calculator is conflating the local strategy position with the Alpaca-only MSFT holding because they share the underlying ticker.

## Evidence

`decision/strategist/user_message.md` line 174-186 (debug-pos-07 details):

```
debug-pos-07
  Underlying:    MSFT (instrument: strategy, direction: long)
  Size:          —  $4,920  (4.5% of portfolio)
  P/L:           +$4,895 since open (+19900.0%)
  Age:           0.0 hours (placed 2026-05-19T03:06:54Z)
  ...
```

Note: cost basis $25, current value $4,920 = $4,895 P/L. The $4,920 current value is consistent with 1 share of MSFT at \~$492 close — combined with the RECONCILIATION_ALERT (Alpaca holds MSFT qty=1.0) it's the smoking gun.

`decision/strategist/user_message.md` line 193 (Activity Log):

```
[2026-05-19T03:06:56Z] RECONCILIATION_ALERT: position alpaca_only_position mismatch: local=0.0 vs alpaca=1.0 — Alpaca holds MSFT qty=1.0; no matching local position
```

`decision/strategist/response_initial.md` line 342 (strategist's analysis):

> "The CRITICAL flag in the header should be reconciled against the inconsistent P/L display; the reconciliation alert in the activity log (Alpaca holds MSFT qty=1.0; no matching local position) is the most likely explanation for the display anomaly and warrants engine-side investigation rather than a thesis-driven action."

## Root cause \[hypothesis\]

The P/L calculator for debug-pos-07 (local strategy on MSFT) appears to be including the Alpaca-only MSFT holding's market value in its position-level P/L computation. This happens via:

1. **Cross-ticker P/L pollution**: The P/L calculator may join all holdings matching the position's underlying ticker (MSFT in this case) regardless of whether each holding is tied to the specific local strategy. So the Alpaca-only MSFT share's market value gets attributed to debug-pos-07's P/L line.
2. **Missing reconciliation gate**: The strategy position's P/L should be tied to its specific component legs (the strategy is "MSFT bull-call spread" per the thesis). If the P/L calculator can't find the strategy's specific legs but does find an "MSFT holding" matching the underlying, it may default to using that as the P/L source.
3. **Synthetic seed mismatch**: The seed harness may not have properly set up the Alpaca-side state to match the local strategy components, so the reconciliation alert is itself an artifact of the seed not registering all components on the Alpaca side.

## Scope

1. Audit the P/L calculator for positions where `instrument = strategy`. Confirm whether the calculator correctly attributes P/L only to the strategy's specific component legs, or whether it can cross-pollute via shared underlying ticker.
2. Specifically investigate the cross-pollution between debug-pos-07 (local MSFT strategy) and the Alpaca-only MSFT 1-share holding flagged by the RECONCILIATION_ALERT.
3. Confirm what the RECONCILIATION_ALERT handler is supposed to do — should it block P/L computation for affected positions until reconciliation completes, or surface a "P/L unreliable" annotation?
4. If the +19900% is purely a synthetic-seed artifact (Alpaca-side state set up imperfectly by the harness), document this so the verification expectation is clear: in a clean production run, debug-pos-07 would NOT show this P/L.

## Acceptance criteria

- [ ] P/L attribution for `instrument = strategy` positions only sums the strategy's specific component legs, never cross-pollutes via shared underlying ticker
- [ ] When a position is flagged by RECONCILIATION_ALERT, its P/L is either suppressed or annotated as "unreliable pending reconciliation"
- [ ] Regression test: a strategy position with a known cost basis and known leg values produces P/L equal to the leg sum, regardless of any other holdings on the underlying ticker

## Verification

* Read the P/L calculator code path for `instrument = strategy` positions. Confirm whether the SQL/query joins on the strategy's specific leg IDs or on the underlying ticker. Fix if the join is on ticker.
* Unit test the P/L calculator with this scenario: a strategy position on MSFT with cost basis $25 and known leg values summing to $30, plus an unrelated "Alpaca-only" MSFT share at $492 in the same data store. Confirm the P/L calculator returns $5 (from $30 leg-sum minus $25 cost basis), not $4,895.
* Audit the RECONCILIATION_ALERT handler. Confirm it either (a) sets a "P/L unreliable" flag on affected positions, or (b) suppresses P/L computation until reconciled. Add this behavior if absent.
* Query the local positions table and Alpaca-side holdings for MSFT. Manually walk through the P/L calculator's logic on this data; confirm where the cross-pollution enters.

## Notes

This connects to:

* \[\[[ALP-580](https://linear.app/alphamind-jatassi/issue/ALP-580/alp-550-regression-position-with-19900percent-pl-still-triggers)\]\] (\[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\] regression: the +19900% triggers a CRITICAL flag that shouldn't fire on positive P/L)
* \[\[[ALP-579](https://linear.app/alphamind-jatassi/issue/ALP-579/get-exposure-snapshot-returns-no-exposure-despite-8-held-positions)\]\] (get_exposure_snapshot inconsistency: also a state-source discrepancy)

The strategist's adversarial discipline (recognized the +19900% as data artifact, held the position, flagged for engine-side investigation) is the backstop here. The underlying reconciliation bug should be fixed so future invocations don't depend on the strategist catching this pattern.
