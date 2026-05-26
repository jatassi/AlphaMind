# ALP-550 regression — position with +19900% P/L still triggers [🔴 CRITICAL] flag despite signed-comparison fix being marked Done

## Symptom

In invocation `inv-20260519T030654Z-60f10023` (timestamp 2026-05-19T03:06:54Z), the strategist's Position-level constraint proximity block shows `debug-pos-07` with `P/L: +19900.0% of cost (max loss: -80.0%) [🔴 CRITICAL]`. The CRITICAL flag is firing on a position with a **positive** P/L that is the OPPOSITE of a max-loss breach.

This is the exact symptom that \[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\] documented and PR #96 was supposed to fix. \[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\] was marked Done on 2026-05-18T22:18 (\~5 hours before this invocation), so the fix should have been live for this run.

Either:

1. The fix in PR #96 didn't actually address the code path that produces this header rendering
2. The fix landed but a regression was reintroduced afterward
3. The bundle assembler in this invocation is somehow running against pre-fix code

The bug is functionally the same as described in \[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\], but recurring in an invocation that should have had the fix.

## Evidence

`decision/strategist/user_message.md` line 32:

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

debug-pos-07 sits at 4.5% of portfolio (under the 5.0% cap, so no size-based BLOCKED flag from sizing) and P/L is +19900% (positive). The only reason for `[🔴 CRITICAL]` is the max-loss check — and signed comparison would produce no flag because +19900% > -80%.

\[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\] status: `Done`, closed 2026-05-18T22:18:28, with PR #96 ("fix(state-delivery): signed loss-zone for proximity block") attached. \[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\]'s acceptance criteria included: "A position with P/L = +19900% and max_loss = -80% does NOT receive a \[🔴 CRITICAL\] flag."

## Root cause \[hypothesis\]

Two candidate root causes:

1. **PR #96 didn't reach this code path**: The fix may have updated one location where the constraint proximity check is computed but the strategist's input-bundle assembler still uses a separate (unfixed) magnitude-comparison code path. Confirm by reading the actual fix in PR #96 and checking whether all rendering paths for the proximity block use the fixed comparison.
2. **Regression after PR #96 landed**: Some subsequent change between 2026-05-18T22:18 and 2026-05-19T03:06 may have reverted the signed-comparison logic. Check git history for changes to the proximity-rendering module between those timestamps.
3. **Different rendering for strategist vs PM**: If the fix updated the PM's view but not the strategist's view (or vice versa), the bug surface depends on which agent's input the operator is examining.

## Scope

1. Re-open \[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\] or treat this as a follow-up with explicit cross-reference to PR #96.
2. Audit the code path that produces the `Position-level constraint proximity` block in the strategist's input bundle. Confirm whether the signed-comparison fix from PR #96 is applied to this rendering path.
3. Add a regression test specifically using `+19900%` P/L against `-80%` max_loss in the strategist's input bundle assembler. The test should verify NO `[🔴 CRITICAL]` flag.
4. Verify the fix is consistent across analyst, strategist, and PM headers (whichever expose the proximity block).

## Acceptance criteria

- [ ] Position with +19900% P/L and max_loss=-80% does NOT carry `[🔴 CRITICAL]` flag in any agent's header
- [ ] Regression test enforces this in CI
- [ ] If multiple rendering paths exist (analyst/strategist/PM), they all use the same signed-comparison logic

## Verification

* Read PR #96's diff. Identify which file(s) and function(s) the signed-comparison fix touches. Confirm whether the strategist's input bundle assembler calls that fixed function or a different (un-fixed) one.
* Grep the codebase for magnitude-comparison patterns on P/L (e.g., `abs(p/l) >` or `|p/l| >`) — confirm none remain in the proximity-rendering paths.
* Unit test the proximity-block rendering function directly with `(p/l=+19900%, max_loss=-80%)` input; assert no CRITICAL flag.
* Diff the strategist's, analyst's, and PM's header-assembly code paths; confirm they all use the same proximity-block helper.

## Notes

\[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\] is marked Done with PR #96. This filing is a regression report — either the fix didn't fully land or a subsequent change reverted it. Cross-reference: original \[\[[ALP-550](https://linear.app/alphamind-jatassi/issue/ALP-550/position-level-constraint-proximity-uses-magnitude-comparison-positive)\]\] and PR #96 ("fix(state-delivery): signed loss-zone for proximity block").
