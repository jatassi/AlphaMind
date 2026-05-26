# Past-dated polymarket contracts unflagged in synthesizer brief — QR input has [STALE] flagging but synthesizer's qual.prediction_market_delta section does not

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the qualitative researcher's PREDICTION-MARKET SNAPSHOT block applies a `[STALE — LIKELY RESOLVED]` flag to contracts whose resolution date has passed or whose underlying event is no longer relevant. For example:

```
[0xad11c116316e3230412ddfc829ede72f78941986247df4f2eb08c48aba8747c9] Will Crude Oil reach a new all-time high by May 31? ... expires=2026-05-31T00:00:00Z [LOW LIQUIDITY] [STALE — LIKELY RESOLVED]
```

But the synthesizer's `qual.prediction_market_delta` section in its UNIVERSAL CONTEXT block (which is the source for downstream agents) does NOT carry the stale flagging. For instance, the contract "Will Iran close its airspace by May 6?" (with a description referencing a May 6 past date but contract expiration on 2026-05-31) appears in the synthesizer brief without any stale annotation:

```
0x1460e5fe8908d91054dc4ded8f66aff8fc4a188eca184681cc32f4d1148e729a:
    category: conflict
    description: Iran closes its airspace by May 6?
    ...
    yes_probability: 0.0005
```

No `[STALE]` or `[LIKELY RESOLVED]` flag, despite the contract description clearly referencing a date 13 days in the past (May 6, vs invocation May 19).

Downstream agents consuming the synthesizer brief cannot distinguish "live contract" from "past-resolution-date contract" — the description text is the only signal, and it requires the agent to parse natural language for date references.

## Evidence

**QR input has stale flagging** (`analysis/qualitative_researcher/user_message.md` lines 227-242):

```
[0x2c03c41e5032364cb7166e842bdd22913f28aa2c837d8432704aa89c338875c8] Will Pete Buttigieg win the 2028 US Presidential Election? ... [LOW LIQUIDITY] [STALE — LIKELY RESOLVED]
[0x445895d68e167bf457e645ceab3b3e45661ea5d90d5453ede6d107e651377e4b] Will Russia capture all of Kostyantynivka by June 30, 2026? ... [LOW LIQUIDITY] [STALE — LIKELY RESOLVED]
...
```

**Synthesizer brief lacks stale flagging** (`analysis/synthesizer/user_message.md` lines 178-190):

```
0x1460e5fe8908d91054dc4ded8f66aff8fc4a188eca184681cc32f4d1148e729a:
    category: conflict
    description: Iran closes its airspace by May 6?
    ...
    yes_probability: 0.0005
```

The synthesizer brief's `qual.prediction_market_delta` section is a YAML-formatted dump of contract details. The stale-flagging logic that the QR input adopted is not applied here.

## Root cause \[hypothesis\]

The QR input bundle and the synthesizer's brief read prediction-market data through separate code paths:

1. **QR input has a stale-detection post-processor**: After fetching contracts, QR applies a filter/flagger that detects past-resolution-date contracts and low-liquidity contracts, and emits the `[STALE — LIKELY RESOLVED]` / `[LOW LIQUIDITY]` flags.
2. **Synthesizer brief reads prediction-market data raw**: The synthesizer's `qual.prediction_market_delta` section reads from the same prediction-market data store but without applying the stale-detection post-processor. The data passes through as-is.

The fix is to either (a) apply the same stale-detection logic in the synthesizer brief assembler, OR (b) move the stale-detection logic to the prediction-market data layer so both consumers get the same flagged output.

## Scope

1. Locate the QR input's prediction-market stale-detection / low-liquidity flagging logic. Confirm whether it's a post-processor or a data-layer query.
2. Apply the same logic to the synthesizer brief's `qual.prediction_market_delta` section so it carries `[STALE]` / `[LOW LIQUIDITY]` flags consistent with the QR input.
3. Consider moving the stale-detection to the data layer (so all consumers get the same flagged output), or factor out a shared helper.
4. Define "stale" precisely: is it (a) contract description references a date in the past, (b) contract expiration is in the past, (c) yes_probability is close to 0 or 1 and unchanging for N invocations (likely-resolved heuristic), or (d) all of the above?

## Acceptance criteria

- [ ] Synthesizer brief's `qual.prediction_market_delta` section emits `[STALE]` / `[LOW LIQUIDITY]` flags consistent with the QR input's flagging
- [ ] Stale-detection criteria documented in code or design
- [ ] Unit test with a past-dated contract confirms both QR input and synthesizer brief apply the flag

## Verification

* Read the QR input assembler's stale-flagging post-processor. Identify the criteria it applies (past-resolution date / low liquidity / unchanged price).
* Apply the same criteria in the synthesizer brief assembler's `qual.prediction_market_delta` rendering path.
* Unit test the synthesizer brief assembler with synthetic contract data: (a) live contract / (b) past-resolution-date contract / (c) low-liquidity contract. Confirm each produces the appropriate flag in the YAML output.
* Inspect a future synthesizer brief and diff its `qual.prediction_market_delta` section against the QR input's PREDICTION-MARKET SNAPSHOT section — flag annotations should match.

## Notes

The synthesizer's prediction-market section is YAML-formatted, so flag insertion may need format-aware handling (vs the QR input's flat-text format with bracketed flags). One option is to add a `stale: True/False` field per contract in the YAML output and document the convention for downstream agents.
