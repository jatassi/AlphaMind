# CSCO validation infrastructure gap — analyst's clean fade thesis dropped because borrow_cost_resolver missing for short equity validation

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the analyst identified CSCO short post-earnings fade as the single cleanest standalone catalyst-driven setup not deduped by the held book:

> "**CSCO short** has the cleanest setup not deduped by held book:
>
> * Two independent vantage points: \[SA-TECH-ANOM-1\] technical extension (keltner 2.201, RSI 88.46) + \[AR-1\] confirmation (+37% 20d, zero EPS revisions, 1.58x approach volume, stable borrow)
> * Hard catalyst: May 20 print within 24h
> * Fade thesis works even on in-line/modest beat per AR-1's 'textbook' framing"

The analyst attempted to validate this through `validate_guardrail` but the call failed because the validation infrastructure lacks the required resolver for CSCO short equity:

> "Short equity validation infrastructure isn't available for CSCO this cycle. Let me try a defined-risk expression via long puts — options headroom shows 39.9% room.CSCO is not in the validation infrastructure's market price data this cycle, and short equity validation lacks a borrow_cost_resolver. The thesis cannot be validated in any expression — per tool policy, I cannot emit a proposal that hasn't passed validation."

The analyst correctly followed tool policy (no proposal without validation), so the thesis was dropped. But the underlying issue is an infrastructure gap: the only candidate that met all the analyst's quality criteria couldn't be validated for an infrastructure reason rather than a thesis reason.

## Evidence

`decision/analyst/response_initial.md` lines 13-26 (analyst's reasoning):

```
**CSCO short** has the cleanest setup not deduped by held book:
- Two independent vantage points: [SA-TECH-ANOM-1] technical extension (keltner 2.201, RSI 88.46) + [AR-1] confirmation (+37% 20d, zero EPS revisions, 1.58x approach volume, stable borrow)
- Hard catalyst: May 20 print within 24h
- Fade thesis works even on in-line/modest beat per AR-1's "textbook" framing
- Conviction capped at 3: pre-print binary risk + universally absent options flow corroboration

...

Let me validate the CSCO short.Short equity validation infrastructure isn't available for CSCO this cycle. Let me try a defined-risk expression via long puts — options headroom shows 39.9% room.CSCO is not in the validation infrastructure's market price data this cycle, and short equity validation lacks a borrow_cost_resolver. The thesis cannot be validated in any expression — per tool policy, I cannot emit a proposal that hasn't passed validation.
```

`decision/analyst/response_initial.md` line 29 (final disposition):

```
- **CSCO short post-earnings fade**: only candidate with a hard catalyst not deduped by the held book, but cannot be validated this cycle. Drop.
```

So the analyst's failure to emit a proposal in this invocation is partly explained by the validation infrastructure gap, not by absence of a real thesis candidate.

## Root cause \[hypothesis\]

The `validate_guardrail` tool requires multiple resolvers to validate a position:

* Market price resolver (for CSCO market price data)
* Borrow cost resolver (for short equity expressions)
* Options chain resolver (for options expressions)

For this invocation:

* **Market price data**: CSCO market price data appears to be missing in the validation tool's data scope. CSCO IS in the active universe (it's listed in the synthesizer brief with full technical data including price), so the validation tool's data scope is narrower than the universe.
* **Borrow cost resolver**: Short equity validation requires a borrow cost lookup. This resolver appears to be unwired or not initialized for CSCO. The adaptive researcher's `sec_lending` tool call for CSCO returned "borrow cost trend 'stable' and all quantitative fields null" — confirming the borrow cost data store is sparse / partially populated.
* **Options validation**: The defined-risk options expression (long puts) also failed, which suggests options chain resolution is also missing for CSCO.

## Scope

1. Audit the `validate_guardrail` tool's resolver coverage. What's the gap between "tickers in the active universe" and "tickers the validation tool can resolve"?
2. Specifically for short equity: is `borrow_cost_resolver` wired for all single-name tickers in the universe, or is it limited to a subset? If subset, document and surface the limitation explicitly when validation fails for an out-of-scope ticker.
3. Specifically for options: is options chain data available for all earnings-adjacent tickers, or is there a coverage gap?
4. When `validate_guardrail` fails due to missing resolver (not due to a true breach), surface this as a distinct error code so the analyst can distinguish "thesis would breach a guardrail" from "validation infrastructure can't reason about this ticker".
5. Confirm whether the missing resolvers / data scope are time-of-day dependent (e.g., off-hours) or persistent gaps.

## Acceptance criteria

- [ ] `validate_guardrail` tool's resolver coverage is documented (which tickers / instrument types it can validate)
- [ ] Validation failures due to missing resolver vs true breach are distinguishable in the tool response
- [ ] Active-universe tickers (per resolved_config.json sectors) have validation coverage for the instrument types the agents are likely to propose (equity long/short, options long)
- [ ] If borrow cost is genuinely unavailable for a ticker, validation surfaces this with the right error code rather than a generic FAIL

## Verification

* Audit the resolver-registration code paths for `validate_guardrail`. Confirm coverage for each (market_price, borrow_cost, options_chain) against the 66 active-universe single-name tickers.
* For CSCO specifically: query the borrow_cost data store and options-chain store; confirm row counts and whether the gap is "no rows" or "rows exist but resolver fails to read them".
* Unit test `validate_guardrail` with three synthetic scenarios: (a) ticker fully resolvable / (b) ticker missing borrow_cost / (c) ticker missing options chain. Confirm the response is distinguishable per case (distinct error codes, not generic FAIL).
* Manually invoke `validate_guardrail` via the MCP tool interface for CSCO short equity; confirm the response now includes an explicit "missing_borrow_cost_resolver" or equivalent error.

## Notes

This is a Medium-severity bug because:

* The analyst's tool-policy discipline (no proposal without validation) is correct and prevented a wrong outcome
* BUT the policy failure mode (drop the only clean candidate) is suboptimal when the validation gap is infrastructure rather than guardrail
* Coverage of the validation tool is a fundamental capability question: every active-universe ticker should be validateable, or the agent should be told which subset is excluded

Related to \[\[[ALP-567](https://linear.app/alphamind-jatassi/issue/ALP-567/news-pipeline-failure-at-0306-utc-0-headlines-collected-across-all)\]\] (news pipeline gap at 03:06 UTC) and \[\[[ALP-579](https://linear.app/alphamind-jatassi/issue/ALP-579/get-exposure-snapshot-returns-no-exposure-despite-8-held-positions)\]\] (get_exposure_snapshot inconsistency) — multiple tool/data scope gaps surface at off-hours invocations. May share a common root in the snapshot / state-source coverage.
