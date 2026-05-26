# get_exposure_snapshot returns "No exposure" despite 8 held positions — cascades to all decision-agent headroom displays as 0%

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the `get_exposure_snapshot` MCP tool returns "No exposure (all positions flat or empty book)" while `get_positions_summary` (called in the same invocation) returns 8 held positions. The two tools are directly contradictory views of the same portfolio state.

The synthesizer explicitly surfaced this in its response:

> "The `get_exposure_snapshot` tool returned 'No exposure (all positions flat or empty book)' — directly contradicting the eight held positions returned by `get_positions_summary`; the sector-level exposure tool output is unreliable this invocation and should not be used to characterize portfolio alignment."

The bug propagates downstream into every decision agent's guardrail-state header. All three agents (analyst, strategist, PM) show:

```
Sector headroom (delta-adjusted):
  Energy:     0.0% / 25.0% — room: 25.0% [NORMAL]
  Financials: 0.0% / 25.0% — room: 25.0% [NORMAL]
  Semis:      0.0% / 25.0% — room: 25.0% [NORMAL]
  Tech:       0.0% / 25.0% — room: 25.0% [NORMAL]

Directional headroom:
  Net long:  0.0% / 60.0% — room: 60.0%
  Net short: 0.0% / 30.0% — room: 30.0%
  Gross:     0.0% / 120.0% — room: 120.0%
```

These show 0% usage despite real holdings: COP at 13.6% Energy, JPM at 17.3% Financials, NVDA at 16.9% Semis, multiple Tech positions totaling \~30%. The aggregate exposure should show \~78% gross, \~50% net long — instead it shows 0% across the board.

The strategist also independently noted: "validation tool's state view appears inconsistent with the guardrail header (it shows current portfolio as flat, but the header confirms 8 over-sized positions). The position_max_size_pct FAIL appears to be a tool-side state mismatch — the tool doesn't see the existing positions, so it can't properly model a reduce-toward-compliance." So the bug also affects `validate_guardrail` tool calls.

Real downstream consequence: in this invocation, the strategist correctly identified the inconsistency and proceeded based on the (correct) "Held positions" listing in the header. But the validation tool's projected-state checks return FAIL on positions the tool doesn't know about, so the strategist had to manually override and flag for engine-side reconciliation. In production, a downstream consumer that trusts `get_exposure_snapshot` would issue trades based on a "flat book" assumption while a real book exists.

## Evidence

`analysis/synthesizer/response.md` line 67 (final paragraph, "Structural data degradation"):

> "The `get_exposure_snapshot` tool returned 'No exposure (all positions flat or empty book)' — directly contradicting the eight held positions returned by `get_positions_summary`; the sector-level exposure tool output is unreliable this invocation and should not be used to characterize portfolio alignment."

`decision/analyst/user_message.md` lines 8-17 (broken headroom):

```
Sector headroom (delta-adjusted):
  Energy:     0.0% / 25.0% — room: 25.0% [NORMAL]
  Financials: 0.0% / 25.0% — room: 25.0% [NORMAL]
  Semis:      0.0% / 25.0% — room: 25.0% [NORMAL]
  Tech:       0.0% / 25.0% — room: 25.0% [NORMAL]

Directional headroom:
  Net long:  0.0% / 60.0% — room: 60.0%
  Net short: 0.0% / 30.0% — room: 30.0%
  Gross:     0.0% / 120.0% — room: 120.0%
```

`decision/strategist/user_message.md` lines 68-72 (aggregate state showing 0):

```
Aggregate:
  Portfolio P/L: intraday $0, cumulative realized $0
  Drawdown: daily 0.0% [NORMAL], cumulative 0.0% [NORMAL]
  Net long: $0 (0.0% of portfolio)
  Gross:    $0 (0.0% of portfolio)
```

`decision/strategist/response_initial.md` line 1:

> "The validation tool's state view appears inconsistent with the guardrail header (it shows current portfolio as flat, but the header confirms 8 over-sized positions). The position_max_size_pct FAIL appears to be a tool-side state mismatch — the tool doesn't see the existing positions, so it can't properly model a reduce-toward-compliance."

For contrast, the analyst's "Held positions" block (which uses `get_positions_summary`) correctly shows all 8 positions.

## Root cause \[hypothesis\]

`get_positions_summary` and `get_exposure_snapshot` read from different underlying state sources. Likely candidates:

1. **Different snapshot reads**: `get_positions_summary` reads from a snapshot that includes positions; `get_exposure_snapshot` reads from a snapshot that doesn't. The recent snapshot-pipeline work (\[\[[ALP-449](https://linear.app/alphamind-jatassi/issue/ALP-449/pipeline-scheduler-wire-snapshotbackedsynthesizerreader-resolve-cross)\]\]) may have left an `_EmptySynthesizerReader` stub in the path that `get_exposure_snapshot` traverses.
2. **Exposure calculator bypasses positions**: The exposure calculator may compute from a cash/equity aggregate that wasn't synced after positions were placed, while `get_positions_summary` reads directly from the positions table.
3. `validate_guardrail` **shares the same broken path**: The strategist noted that validation calls return FAIL on positions the tool doesn't know about — confirming the validation tool reads from the same broken state source as `get_exposure_snapshot`.

## Scope

1. Audit the `get_exposure_snapshot` MCP tool's implementation. Confirm what state source it reads and why it returns "No exposure" when positions exist.
2. Audit the `validate_guardrail` MCP tool — confirm whether it reads from the same broken source or a different one. Strategist's narrative suggests same source.
3. Audit the headroom calculator that produces the `Sector headroom / Directional headroom / Aggregate` blocks in the agent input bundles. Confirm same source.
4. Wire all three to the snapshot source that `get_positions_summary` correctly reads from.
5. Add an integration test: with N held positions, all three tools return non-zero exposure consistent with the positions.

## Acceptance criteria

- [ ] `get_exposure_snapshot` returns non-zero exposure when positions exist
- [ ] `validate_guardrail` returns PASS / appropriate result on positions the tool knows about
- [ ] Agent input bundles' headroom blocks reflect held positions: e.g., "Energy: 13.6% / 25.0%" not "0.0% / 25.0%"
- [ ] Aggregate state block reflects held positions: e.g., "Net long: $XX,XXX (XX% of portfolio)" not "Net long: $0 (0.0%)"
- [ ] Integration test with N held positions verifies all three tools return consistent exposure

## Verification

* Read the implementation code for `get_exposure_snapshot`, `validate_guardrail`, and the headroom calculator. Confirm they all read from the same snapshot source that `get_positions_summary` uses.
* Integration test: seed N held positions; call all three tools; assert each returns non-zero exposure consistent with the seeded positions. Specifically: `get_exposure_snapshot` returns N positions with sector breakdown; `validate_guardrail`'s projected-state check sees the held positions; the agent bundle assembler's headroom block reflects them.
* Query the underlying snapshot/state source directly for the test invocation; confirm position rows are present and being read correctly by all three tools.
* Diff `get_positions_summary` output against `get_exposure_snapshot` output in a unit test with seeded positions — they should describe the same state.

## Notes

This is the highest-severity bug in this invocation — silent data integrity failure where one tool says "8 positions" and another says "0 positions" from the same portfolio state. The strategist's adversarial discipline is the only thing preventing this from cascading into wrong trade decisions. A future invocation where the bug manifests but the strategist doesn't catch it would issue trades based on a "flat book" assumption while a real book exists.

This may be related to \[\[[ALP-449](https://linear.app/alphamind-jatassi/issue/ALP-449/pipeline-scheduler-wire-snapshotbackedsynthesizerreader-resolve-cross)\]\] (SnapshotBackedSynthesizerReader) — the snapshot reader work was completed but the exposure tool may still traverse an old path.
