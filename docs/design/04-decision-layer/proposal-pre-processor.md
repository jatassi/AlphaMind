# Proposal pre-processor

Deterministic processing step between analyst/strategist outputs and the portfolio manager. Reads the structured fields from both agents' outputs, computes cross-proposal observations, wraps each agent's outputs with per-recommendation annotations, and delivers the consolidated bundle to the PM. Pure computation on typed data.

Formal JSON Schema: [proposal-pre-processor-bundle-schema.md](proposal-pre-processor-bundle-schema.md). The data shapes below are the readable reference.

**Why this exists:** Without the pre-processor, the PM would mentally cross-reference proposals from two agents to detect conflicts, compute cumulative capital and exposure impact, and assess aggregate book health. These mechanical operations an LLM can get wrong under cognitive load. The pre-processor makes them deterministic so the PM can focus on judgment — thesis quality, portfolio coherence, sizing.

---

## Inputs

- All analyst recommendations — the analyst's full output document (structured + narrative fields), conforming to [`analyst-output-schema.md`](analyst-output-schema.md)
- All strategist position assessments and pending-order assessments — the strategist's full output document (structured + narrative fields), conforming to [`strategist-output-schema.md`](strategist-output-schema.md)
- Current portfolio state snapshot — positions, sector exposures, capital, guardrail headroom from the same Phase 1 snapshot the analyst and strategist consumed

---

## Output

A single bundle to the PM with three top-level sections:

| Section | Content |
|---|---|
| **§1 Aggregate observations** | Three deterministic observations across the combined proposal set: combined-set impact, conviction distribution, book health summary |
| **§2 Strategist section** | The strategist's mode, position assessments and pending-order assessments wrapped with per-record annotations, and portfolio-level observations passed through. Preserves the strategist's presentation order |
| **§3 Analyst section** | The analyst's mode plus mode-conditional content: wrapped recommendations (normal mode) or watchlist entries passed through (watchlist mode). Preserves the analyst's presentation order |

Strategist before analyst: existing-book decisions (close/reduce/add) change capital and headroom new entries fit into. The PM's natural workflow is "manage the book, then evaluate additions."

Wrappers preserve the inner record verbatim — it remains independently validatable against the agent's output schema.

```json
{
  "invocation_id": "...",
  "timestamp": "...",
  "aggregate_observations": { ... },
  "strategist_section": {
    "mode": "...",
    "position_assessments": [ ... ],
    "pending_order_assessments": [ ... ],
    "portfolio_level_observations": { ... }
  },
  "analyst_section": {
    "mode": "...",
    "recommendations": [ ... ]
  }
}
```

---

## §1 — Aggregate observations

Three observations derived deterministically from the combined set.

### §1.A `combined_set_impact`

Per-rule projection of the combined set's effect on guardrail state, computed via the shared [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) — the same primitives the agent-side validation tool uses. The per-rule shape is the library's canonical output object.

```json
{
  "basis": {
    "analyst_proposal_ids": ["REC-1", "REC-2", "REC-3"],
    "strategist_action_ids": ["SA-1", "SA-3", "SA-5"],
    "strategist_holds_excluded_count": 7,
    "snapshot_timestamp": "2026-04-25T14:00:00Z"
  },
  "per_rule": [
    {
      "rule": "sector_concentration_tech",
      "status": "PASS",
      "current": 18.3,
      "limit": 25.0,
      "projected_after": 22.1,
      "headroom_remaining": 2.9,
      "unit": "% of portfolio (delta-adjusted)"
    },
    {
      "rule": "net_long_exposure",
      "status": "FAIL",
      "current": 42.0,
      "limit": 50.0,
      "projected_after": 52.5,
      "headroom_remaining": -2.5,
      "unit": "% of portfolio"
    }
  ],
  "breaches": [
    {
      "rule": "net_long_exposure",
      "overage": 2.5,
      "unit": "% of portfolio",
      "contributors": [
        {"proposal_id": "REC-2", "contribution": 5.0},
        {"proposal_id": "REC-3", "contribution": 4.0},
        {"proposal_id": "SA-5", "contribution": 1.5}
      ]
    }
  ]
}
```

- **`basis`** identifies what's included. Strategist holds are excluded from the math (no exposure change); the count is reported for context.
- **`per_rule`** has one entry per rule active under the current portfolio profile and regime. Rule IDs come from [`rules-and-limits.md`](../06-risk-guardrails/rules-and-limits.md). The `status` enum (`PASS | WARNING | FAIL`) and remaining fields match the validation tool's per-rule shape.
- **`breaches`** is the slice of `per_rule` where `status ≠ PASS`, augmented with `contributors` — signed per-proposal attribution that only makes sense in batch. Negative contributions indicate proposals pulling the rule away from breach (e.g., a close on a long contributes negatively to net long exposure).
- Capital sits in `per_rule` like any other rule (`available_capital`). The combined-set projection covers it the same way it covers sector concentration, directional exposure, gross exposure, and Greeks.

### §1.B `conviction_distribution`

```json
{
  "by_level": {"1": 0, "2": 1, "3": 3, "4": 1, "5": 0},
  "total": 5
}
```

Histogram of analyst recommendations by conviction level. Calibration drift is tracked across invocations by the feedback loop (see [conviction-scale calibration target](analyst.md#conviction-scale)).

### §1.C `book_health_summary`

```json
{
  "by_thesis_status": {
    "on-track": 6,
    "partially-realized": 1,
    "at-risk": 2,
    "stale": 1,
    "invalidated": 0
  },
  "by_recommended_action": {
    "hold": 7,
    "reduce": 1,
    "close": 1,
    "adjust-bracket": 1,
    "add": 0
  },
  "remedy_flagged_count": 1,
  "total": 10
}
```

Two histograms over the strategist's per-position assessments plus a remedy-flagged count. An at-a-glance scoreboard the PM reads before diving into individual assessments. Status × action correlation is read directly from §2.

---

## §2 — Strategist section

```json
{
  "mode": "normal" | "defensive_posture",
  "position_assessments": [
    {
      "assessment": <strategist's SA-N record, validates against strategist-output-schema.md $defs/position_assessment unmodified>,
      "pre_processor_annotations": {
        "conflicts": [
          {
            "with_recommendation_id": "REC-3",
            "underlying": "NVDA",
            "conflict_type": "entry_vs_close"
          }
        ]
      }
    }
  ],
  "pending_order_assessments": [
    {
      "pending_order_assessment": <strategist's SA-ORD-N record, validates against strategist-output-schema.md $defs/pending_order_assessment unmodified>,
      "pre_processor_annotations": {
        "conflicts": []
      }
    }
  ],
  "portfolio_level_observations": <strategist's portfolio_level_observations object, passed through verbatim>
}
```

`mode` is read from the strategist's output. `position_assessments` are wrapped with conflict annotations against analyst recommendations. `pending_order_assessments` use the same annotation envelope; auto-detection runs for unfilled entry orders (`entry_limit`, `entry_stop_limit`) and skips bracket legs (`bracket_target`, `bracket_price_stop`, `bracket_time_stop`, `bracket_event_stop`) — bracket-leg conflicts are captured by the parent position's assessment. `portfolio_level_observations` is passed through verbatim.

§2 preserves the [strategist's presentation order](strategist.md#presentation-order-and-token-budget) (status severity → action urgency → portfolio weight, with remedy-flagged first within tier; pending orders by action then age).

## §3 — Analyst section

In normal mode:

```json
{
  "mode": "normal",
  "recommendations": [
    {
      "recommendation": <analyst's REC-N record, validates against analyst-output-schema.md $defs/recommendation unmodified>,
      "pre_processor_annotations": {
        "conflicts": [
          {
            "with_assessment_id": "SA-7",
            "underlying": "NVDA",
            "conflict_type": "entry_vs_close"
          }
        ]
      }
    }
  ]
}
```

In watchlist mode (halt active):

```json
{
  "mode": "watchlist",
  "watchlist": [<analyst's watchlist_entry records, passed through verbatim>]
}
```

Watchlist entries don't carry full proposal detail; conflict detection does not apply.

§3 preserves the [analyst's presentation order](analyst.md#presentation-order) (conviction descending → entry-window urgency → risk asymmetry).

## Wrap pattern and conflicts annotation

The wrap pattern keeps "what the agent said" structurally distinct from "what the pre-processor computed about it." The PM's evaluation framework treats agent claims (subject to skepticism, source-brief verification) and infrastructural facts (deterministic) as different epistemological objects; wrapping makes that distinction visible at the data layer.

Conflict annotations are inline, mirror-symmetric, with no separate catalog. Three fields per entry:

| Field | Description |
|---|---|
| Cross-reference | Analyst side: `with_assessment_id` (position assessment) or `with_pending_order_assessment_id` (unfilled entry order). Strategist side: `with_recommendation_id` |
| `underlying` | Root ticker |
| `conflict_type` | Enum classifying the interaction (below) |

`conflict_type` enum:

| Value | Pattern |
|---|---|
| `entry_vs_close` | Analyst proposes entering while strategist recommends closing |
| `entry_vs_add` | Analyst proposes a new position while strategist recommends adding to an existing one |
| `entry_vs_hold` | Analyst proposes a new position in a name where an existing position is held with no action change, and the analyst's direction matches the held position's direction |
| `entry_direction_conflict` | Analyst's direction is opposite to a held position whose strategist assessment is `hold` |
| `entry_vs_pending_maintain` | Analyst proposes a new entry on the same underlying as an unfilled entry order whose strategist assessment is `maintain` |
| `entry_vs_pending_modify` | Analyst proposes a new entry on the same underlying as an unfilled entry order whose strategist assessment is `modify` |
| `entry_vs_pending_cancel` | Analyst proposes a new entry on the same underlying as an unfilled entry order whose strategist assessment is `cancel` |

The `conflicts` array is `[]` when there are no same-underlying interactions. The PM uses cross-reference IDs to read the linked record; the conflict entry is a flag, not a substitute for reading the proposals.

---

## Edge cases

Degenerate inputs handled via empty arrays and zero counts.

| Case | Effect |
|---|---|
| Zero analyst proposals | §3 is `[]`; §1.B is all zeros with `total: 0`; §1.A's `analyst_proposal_ids` is `[]`. No conflicts can exist on §2 entries |
| Zero strategist non-hold actions | §1.A's `strategist_action_ids` is `[]` and `strategist_holds_excluded_count` reflects the full assessment count; §1.C is unchanged. Conflicts on §3 entries can only be `entry_vs_hold` or `entry_direction_conflict` |
| Zero open positions | §2 is `[]`; §1.C is all zeros with `total: 0`; baseline portfolio is mostly cash and §1.A reflects this |
| Both empty | Bundle is still produced for uniformity. PM reads, emits no envelopes, invocation ends |
| Halt mode (analyst watchlist + strategist defensive_posture) | §3 carries `mode: "watchlist"` and the watchlist array; `recommendations` is omitted. §2 carries `mode: "defensive_posture"` and `portfolio_level_observations` includes the required `defensive_posture_summary`. §1.A's `analyst_proposal_ids` is `[]`; conflict detection does not apply |

---

## Token budget

Sizing targets for pipeline orchestration; not behavioral targets.

| Portfolio profile | §1 aggregate observations | Per-entry wrap overhead | Total bundle overhead beyond raw agent outputs |
|---|---|---|---|
| Primary ($1,500, 8–12 active rules) | ~330–530 tokens | ~20–40 tokens | ~360–800 tokens |
| Full-system ($100K, 12–17 active rules) | ~500–750 tokens | ~20–40 tokens | ~700–1,350 tokens |

§1.A dominates §1 — `per_rule` scales with rules active under profile + regime, plus 0–3 breach detail entries (~50 tokens each). §1.B and §1.C are small and fixed (~30 and ~80 tokens).

Wrappers add minimal overhead — the conflicts annotation is small (3 fields per conflict) and `[]` in the typical case. See [analyst.md token budget](analyst.md) and [strategist.md token budget](strategist.md#presentation-order-and-token-budget).

---

## Validation

Unit tests target the cross-proposal computations and wrap/annotation orchestration. Heavier math (delta-adjusted exposure, per-rule evaluation, regime parameter resolution) lives in the shared [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) and is tested at the library level.

| Category | Concern |
|---|---|
| Combined-set impact arithmetic | Per-rule projected values match library output for fixture (current state, proposed actions) tuples |
| Breach detection | When the combined set crosses a limit, the breach entry is populated with correct overage and signed contributors |
| Conviction distribution | Histogram counts match input recommendation set |
| Book health summary | Both histograms and `remedy_flagged_count` match input assessment set |
| Conflict detection | Same-underlying configurations produce the correct `conflict_type` classification and mirror-symmetric annotations: (a) analyst recommendation × strategist position assessment, (b) analyst recommendation × unfilled entry pending-order assessment. Bracket-leg pending orders carry empty conflicts on same-underlying matches because their conflicts are captured by the parent position assessment |
| Presentation order preservation | §2 order matches the strategist's emitted order; §3 order matches the analyst's emitted order |
| Wrap structure integrity | Inner records (`assessment`, `recommendation`) validate against their original schemas unmodified after wrapping |
| Edge cases | Each of the four edge-case scenarios produces a structurally valid bundle |

The integration test plan ([`testing/integration-test-plan.md`](../testing/integration-test-plan.md)) covers cross-agent orchestration scenarios that consume the shapes defined here.

---

## Dependencies

- [Analyst](analyst.md) — structured output fields define the §3 input contract; presentation order is preserved into §3
- [Strategist](strategist.md) — structured output fields define the §2 input contract; presentation order is preserved into §2
- [Portfolio manager](portfolio-manager.md) — primary consumer of the bundle
- [Analyst output schema](analyst-output-schema.md) and [strategist output schema](strategist-output-schema.md) — authoritative contracts for the inner records the wrappers preserve
- [Portfolio state (raw)](../01-data-layer/internal/portfolio-state.md) — provides current positions, sector exposures, capital baseline
- [Risk guardrails / state delivery](../06-risk-guardrails/state-delivery.md) — provides current guardrail headroom; the validation tool's per-rule output shape is reused in §1.A
- [Rules and limits](../06-risk-guardrails/rules-and-limits.md) — canonical rule registry (rule IDs in §1.A's `per_rule` and `breaches` come from here)
- [Guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) — provides the deterministic math (delta-adjusted exposure, per-rule projection, regime parameter resolution, feature-flag early-exit) underlying §1.A; the per-rule output shape is the library's canonical object
