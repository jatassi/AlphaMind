# Analyst guardrail-state view diverges from strategist/PM — sees $5 max + Held=None instead of $4,748 + 8 positions

## Symptom

The analyst, strategist, and PM all receive a guardrail-state header block at the top of their input bundles. In this invocation, the analyst sees a completely different (and internally inconsistent) view of the same portfolio state than the strategist and PM:

| Field | Analyst sees | Strategist/PM see |
| -- | -- | -- |
| Per-position max size | `$5 (0.0% of portfolio)` | `$4,748 (5.0% of portfolio)` |
| Held positions | `None` | 8 positions detailed (debug-pos-00..07) |
| Hard blocks | `Position max size at 5.0% / 5.0% limit` | `Position max size at 5.0% / 5.0% limit` (same — but warranted in strategist's view because positions over cap exist) |

The analyst view is internally inconsistent:

* "Available for new positions: $24,440 (25.7% of portfolio)" implies a \~$95k portfolio. 5% of that should be \~$4,750, not $5.
* "Held positions: None" — but Hard Blocks says position max size is at 5.0% / 5.0% (100% utilized of the cap). Utilization of 100% with zero held positions is paradoxical.

The result: the analyst correctly concluded that any non-trivial proposal would fail per-position validation and emitted an empty recommendations array. The output was coincidentally correct (zero proposals is a valid outcome), but the reasoning was based on broken state — in any future invocation where the bug manifests but a proposal is warranted, the analyst would still bail.

## Evidence

E2E invocation: `/Volumes/Users/jacks/AlphaMind/.archive/verify-debug-e2e/invocations/inv-20260518T111140Z-7b54d0d6/`

`decision/analyst/user_message.md` lines 1-32 — analyst's broken view:

```
=== GUARDRAIL STATE (invocation inv-20260518T111140Z-7b54d0d6, 2026-05-18T11:11:40Z) ===
Regime: normal [unchanged]

Capital:
  Available for new positions: $24,440 (25.7% of portfolio)
  Per-position max size: $5 (0.0% of portfolio, normal regime)

Sector headroom (delta-adjusted):
  Energy:     0.0% / 25.0% — room: 25.0% [NORMAL]
  Financials: 0.0% / 25.0% — room: 25.0% [NORMAL]
  Semis:      0.0% / 25.0% — room: 25.0% [NORMAL]
  Tech:       0.0% / 25.0% — room: 25.0% [NORMAL]

Directional headroom:
  Net long:  0.0% / 60.0% — room: 60.0%
  Net short: 0.0% / 30.0% — room: 30.0%
  Gross:     0.0% / 120.0% — room: 120.0%

Options headroom:
  Delta exposure: 0.1% / 40.0% — room: 39.9%

Held positions (dedup — skip same underlying + direction; strategist owns hold/add/reduce):
  None

Hard blocks (do NOT recommend):
  Position max size at 5.0% / 5.0% limit
===
```

`decision/strategist/user_message.md` lines 1-60 — strategist's correct view:

```
Capital:
  Available for new positions: $24,440 (25.7% of portfolio)
  Per-position max size: $4,748 (5.0% of portfolio, normal regime)

[... sector / directional / options headroom identical to analyst's ...]

Position-level constraint proximity:
  debug-pos-00: 16.9% of portfolio (max 5.0%) — P/L: +0.0% of cost ... [BLOCKED]
  debug-pos-01: 17.3% of portfolio (max 5.0%) ... [BLOCKED]
  [... 6 more positions ...]
```

`decision/portfolio_manager/user_message.md` lines 1-50 — PM matches strategist exactly. Analyst is the lone outlier.

Analyst response (`decision/analyst/response_initial.md` lines 3-4) cites the broken state as the primary justification for bailing:

> *"Per-position max size is effectively zero. The header reports Per-position max size: $5 (0.0% of portfolio) and the Hard blocks block explicitly states Position max size at 5.0% / 5.0% limit. Any equity or options proposal of meaningful size would fail per-position validation; there is no compliant sizing that preserves a real thesis."*

## Root cause hypothesis

Two possibilities, both pointing at the analyst's guardrail-state assembler being a different code path than the strategist/PM's:

**Hypothesis A (likely): dedup-skip artifact.**
The analyst's view header literally says *"Held positions (dedup — skip same underlying + direction; strategist owns hold/add/reduce)"*. The analyst's assembler may be applying a dedup pass that's supposed to filter out positions already owned by the strategist's hold/add/reduce mandate. If that pass is buggy — e.g., it's removing the positions from the `Held positions` block but the per-position max calculation still includes them — that explains:

* "Held positions: None" (dedup filtered them all)
* "Per-position max size: $5" (calculation still includes the 5%-cap usage from the existing 16.9%+17.3%+12.7%+13.6%+6.8%+4.0%+2.0%+4.5% = 78% of portfolio in over-cap positions, leaving only \~$5 of unused per-position budget)
* Hard block firing despite Held=None (block check sees the over-cap positions even though the listing block doesn't)

**Hypothesis B (possible): unit/formatting bug.**
The "$5" is a formatting truncation of $4,748 (e.g., a printf bug rendering a wide integer as $X). Less likely because the percentage rendering "(0.0% of portfolio)" also reflects a tiny value, not a percentage of $4,748.

Hypothesis A is the more coherent explanation.

## Scope

(a) Audit the analyst's guardrail-state assembler (separate from the strategist/PM assembler) and confirm whether the dedup pass is responsible for the divergence. Likely lives near a helper that constructs the per-agent guardrail header block.

(b) Either:

* Make the analyst assembler use the same code path as the strategist/PM, with a different `Held positions` filter applied at the rendering layer only (not at the per-position-max calculation layer), OR
* Apply the dedup uniformly such that the per-position max calculation reflects the same set as the held-positions listing.

(c) Ensure internal consistency: if Held = None, Hard Blocks for per-position size should not fire.

## Acceptance criteria

- [ ] Analyst's `Per-position max size` matches strategist/PM's value for the same invocation (currently $4,748 in inv-20260518T111140Z).
- [ ] If `Held positions: None`, Hard Blocks does not fire `Position max size at 5.0% / 5.0% limit`.
- [ ] Analyst view shows internally consistent capital + per-position-max + held-positions relationship.

## Verification

* Re-run debug-e2e; diff the `=== GUARDRAIL STATE ===` blocks between analyst, strategist, and PM. The Capital + Per-position max size lines should be identical across all three.
* Analyst view: either positions appear in the held listing OR the per-position-max reflects zero usage; not both broken.

## Notes

Closely related to [ALP-547](https://linear.app/alphamind-jatassi/issue/ALP-547/analyst-treats-inline-brief-preview-as-canonical-doesnt-fetch-full) (analyst doesn't fetch full brief) — both co-presented in this invocation and reinforced each other (broken guardrail + apparent missing brief → analyst bailed). Independent fixes; either can land first. With both resolved, the analyst should produce real proposals from the same input that currently yields an empty array.
