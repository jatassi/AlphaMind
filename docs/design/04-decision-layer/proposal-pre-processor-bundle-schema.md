# Proposal pre-processor bundle schema

Formal JSON Schema (Draft 2020-12) for the [proposal pre-processor](proposal-pre-processor.md)'s output bundle. This is the machine-readable contract that corresponds to the prose data shapes in `proposal-pre-processor.md`. The [portfolio manager](portfolio-manager.md) consumes the bundle as its primary input package alongside the synthesizer brief, portfolio state, and the guardrail state header from [`state-delivery.md`](../06-risk-guardrails/state-delivery.md).

## Scope

- **Contract surface:** one pre-processor invocation produces one bundle matching this schema.
- **Inner records preserved:** `position_assessments[].assessment`, `pending_order_assessments[].pending_order_assessment`, `portfolio_level_observations`, `recommendations[].recommendation`, and `watchlist[]` entries are byte-for-byte the records the analyst and strategist emitted. They validate against [`analyst-output-schema.md`](analyst-output-schema.md) and [`strategist-output-schema.md`](strategist-output-schema.md) unmodified — the pre-processor does not edit them.
- **Modes:** the bundle's `strategist_section.mode` and `analyst_section.mode` mirror the agents' modes. Halt mode produces `strategist_section.mode = "defensive_posture"` and `analyst_section.mode = "watchlist"`; the schema carries the agents' mode-conditional structure verbatim.
- **Empty-input semantics:** all arrays accept zero-length values. A degenerate bundle with no proposals and no positions is structurally valid.

## Cross-references

| Field | Source |
|---|---|
| `aggregate_observations.combined_set_impact.per_rule[]` shape | [`guardrail-evaluation.md`](../06-risk-guardrails/guardrail-evaluation.md) — canonical per-rule output object shared by every guardrail-check call site |
| `aggregate_observations.combined_set_impact.per_rule[].rule` allowed values | [`rules-and-limits.md`](../06-risk-guardrails/rules-and-limits.md) — canonical rule registry |
| `aggregate_observations.book_health_summary.by_thesis_status` keys | [`thesis-model.md` — thesis status classifications](../05-execution-layer/thesis-model.md#thesis-status-classifications) |
| `aggregate_observations.book_health_summary.by_recommended_action` keys | [`strategist-output-schema.md`](strategist-output-schema.md) `recommended_action` enum |
| `strategist_section.mode` enum | [`strategist.md` — Halt mode and defensive-posture behavior](strategist.md#halt-mode-and-defensive-posture-behavior) |
| `analyst_section.mode` enum | [`state-delivery.md` — analyst / watchlist mode](../06-risk-guardrails/state-delivery.md#analyst--watchlist-mode) |
| `conflict_type` enum | [`proposal-pre-processor.md` — Wrap pattern and conflicts annotation](proposal-pre-processor.md#wrap-pattern-and-conflicts-annotation) |

---

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/proposal-pre-processor-bundle.json",
  "title": "Proposal pre-processor bundle",
  "type": "object",
  "required": [
    "invocation_id",
    "timestamp",
    "aggregate_observations",
    "strategist_section",
    "analyst_section"
  ],
  "properties": {
    "invocation_id": {
      "type": "string",
      "description": "Pipeline invocation. Matches the analyst's and strategist's invocation_id."
    },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "When the pre-processor finalized the bundle."
    },
    "aggregate_observations": { "$ref": "#/$defs/aggregate_observations" },
    "strategist_section": { "$ref": "#/$defs/strategist_section" },
    "analyst_section": { "$ref": "#/$defs/analyst_section" }
  },

  "$defs": {
    "aggregate_observations": {
      "type": "object",
      "required": ["combined_set_impact", "conviction_distribution", "book_health_summary"],
      "properties": {
        "combined_set_impact": { "$ref": "#/$defs/combined_set_impact" },
        "conviction_distribution": { "$ref": "#/$defs/conviction_distribution" },
        "book_health_summary": { "$ref": "#/$defs/book_health_summary" }
      }
    },

    "combined_set_impact": {
      "type": "object",
      "required": ["basis", "per_rule", "breaches"],
      "properties": {
        "basis": {
          "type": "object",
          "required": [
            "analyst_proposal_ids",
            "strategist_action_ids",
            "strategist_holds_excluded_count",
            "snapshot_timestamp"
          ],
          "properties": {
            "analyst_proposal_ids": {
              "type": "array",
              "items": { "type": "string", "pattern": "^REC-[0-9]+$" },
              "description": "Analyst recommendations included in the projection. Empty when mode is watchlist or no recommendations were produced."
            },
            "strategist_action_ids": {
              "type": "array",
              "items": { "type": "string", "pattern": "^SA-[0-9]+$" },
              "description": "Strategist position assessments whose recommended_action is non-hold. Holds do not affect exposure and are excluded from the projection."
            },
            "strategist_holds_excluded_count": {
              "type": "integer",
              "minimum": 0,
              "description": "Number of strategist position assessments excluded from the projection because their recommended_action is hold. Reported for context."
            },
            "snapshot_timestamp": {
              "type": "string",
              "format": "date-time",
              "description": "Phase 1 portfolio state snapshot used as the projection baseline."
            }
          }
        },
        "per_rule": {
          "type": "array",
          "items": { "$ref": "#/$defs/per_rule_entry" },
          "description": "One entry per rule active under the current portfolio profile and regime."
        },
        "breaches": {
          "type": "array",
          "items": { "$ref": "#/$defs/breach_entry" },
          "description": "The slice of per_rule where status is FAIL, augmented with signed contributor attribution. Empty when nothing breaches."
        }
      }
    },

    "per_rule_entry": {
      "type": "object",
      "required": ["rule", "status", "current", "limit", "projected_after", "headroom_remaining", "unit"],
      "properties": {
        "rule": {
          "type": "string",
          "description": "Canonical rule identifier from rules-and-limits.md (e.g., 'sector_concentration_tech', 'net_long_exposure', 'gross_exposure', 'available_capital', 'options_delta_exposure')."
        },
        "status": {
          "enum": ["PASS", "WARNING", "FAIL"],
          "description": "Per-rule decision after applying the combined set. Matches the validation tool's per-rule status enum."
        },
        "current": {
          "type": "number",
          "description": "Rule value before applying the combined set."
        },
        "limit": {
          "type": "number",
          "description": "Rule limit under the active regime."
        },
        "projected_after": {
          "type": "number",
          "description": "Rule value after applying the combined set."
        },
        "headroom_remaining": {
          "type": "number",
          "description": "limit - projected_after. Negative when status is FAIL."
        },
        "unit": { "type": "string" }
      }
    },

    "breach_entry": {
      "type": "object",
      "required": ["rule", "overage", "unit", "contributors"],
      "properties": {
        "rule": {
          "type": "string",
          "description": "Matches the rule field of a per_rule entry whose status is FAIL."
        },
        "overage": {
          "type": "number",
          "description": "projected_after - limit. Always positive for a breach."
        },
        "unit": { "type": "string" },
        "contributors": {
          "type": "array",
          "items": { "$ref": "#/$defs/contributor_entry" },
          "description": "Signed per-proposal attribution. Computed by the pre-processor (the validation tool computes one proposal at a time and does not produce this attribution)."
        }
      }
    },

    "contributor_entry": {
      "type": "object",
      "required": ["proposal_id", "contribution"],
      "properties": {
        "proposal_id": {
          "type": "string",
          "pattern": "^(REC|SA)-[0-9]+$",
          "description": "Either an analyst recommendation ID (REC-N) or a strategist position assessment ID (SA-N)."
        },
        "contribution": {
          "type": "number",
          "description": "Signed contribution to the breaching rule's projected value. Positive when the proposal pushes the rule toward breach; negative when it pulls the rule away (e.g., a close on a long position contributes negatively to net long exposure)."
        }
      }
    },

    "conviction_distribution": {
      "type": "object",
      "required": ["by_level", "total"],
      "properties": {
        "by_level": {
          "type": "object",
          "required": ["1", "2", "3", "4", "5"],
          "properties": {
            "1": { "type": "integer", "minimum": 0 },
            "2": { "type": "integer", "minimum": 0 },
            "3": { "type": "integer", "minimum": 0 },
            "4": { "type": "integer", "minimum": 0 },
            "5": { "type": "integer", "minimum": 0 }
          },
          "additionalProperties": false,
          "description": "Histogram of analyst recommendations by conviction level. Empty (all zeros) when mode is watchlist or no recommendations were produced."
        },
        "total": {
          "type": "integer",
          "minimum": 0,
          "description": "Sum of by_level counts. Equals the length of analyst_section.recommendations."
        }
      }
    },

    "book_health_summary": {
      "type": "object",
      "required": ["by_thesis_status", "by_recommended_action", "remedy_flagged_count", "total"],
      "properties": {
        "by_thesis_status": {
          "type": "object",
          "required": ["on-track", "partially-realized", "at-risk", "stale", "invalidated"],
          "properties": {
            "on-track": { "type": "integer", "minimum": 0 },
            "partially-realized": { "type": "integer", "minimum": 0 },
            "at-risk": { "type": "integer", "minimum": 0 },
            "stale": { "type": "integer", "minimum": 0 },
            "invalidated": { "type": "integer", "minimum": 0 }
          },
          "additionalProperties": false
        },
        "by_recommended_action": {
          "type": "object",
          "required": ["hold", "reduce", "close", "adjust-bracket", "add"],
          "properties": {
            "hold": { "type": "integer", "minimum": 0 },
            "reduce": { "type": "integer", "minimum": 0 },
            "close": { "type": "integer", "minimum": 0 },
            "adjust-bracket": { "type": "integer", "minimum": 0 },
            "add": { "type": "integer", "minimum": 0 }
          },
          "additionalProperties": false
        },
        "remedy_flagged_count": {
          "type": "integer",
          "minimum": 0,
          "description": "Count of position assessments with remedy_flag populated."
        },
        "total": {
          "type": "integer",
          "minimum": 0,
          "description": "Equals the length of strategist_section.position_assessments."
        }
      }
    },

    "strategist_section": {
      "type": "object",
      "required": [
        "mode",
        "position_assessments",
        "pending_order_assessments",
        "portfolio_level_observations"
      ],
      "properties": {
        "mode": {
          "enum": ["normal", "defensive_posture"],
          "description": "Mirrors the strategist's mode field. Read from the strategist's output."
        },
        "position_assessments": {
          "type": "array",
          "items": { "$ref": "#/$defs/wrapped_position_assessment" },
          "description": "Order preserved from the strategist's output. Empty when no positions are open."
        },
        "pending_order_assessments": {
          "type": "array",
          "items": { "$ref": "#/$defs/wrapped_pending_order_assessment" },
          "description": "Order preserved from the strategist's output. Empty when no pending orders exist."
        },
        "portfolio_level_observations": {
          "$ref": "https://alphamind.local/schemas/strategist-output.json#/$defs/portfolio_level_observations",
          "description": "Passed through verbatim from the strategist's output."
        }
      }
    },

    "wrapped_position_assessment": {
      "type": "object",
      "required": ["assessment", "pre_processor_annotations"],
      "properties": {
        "assessment": {
          "$ref": "https://alphamind.local/schemas/strategist-output.json#/$defs/position_assessment",
          "description": "Strategist's SA-N record, preserved unmodified."
        },
        "pre_processor_annotations": { "$ref": "#/$defs/strategist_side_annotations" }
      }
    },

    "wrapped_pending_order_assessment": {
      "type": "object",
      "required": ["pending_order_assessment", "pre_processor_annotations"],
      "properties": {
        "pending_order_assessment": {
          "$ref": "https://alphamind.local/schemas/strategist-output.json#/$defs/pending_order_assessment",
          "description": "Strategist's SA-ORD-N record, preserved unmodified."
        },
        "pre_processor_annotations": { "$ref": "#/$defs/strategist_side_annotations" }
      }
    },

    "strategist_side_annotations": {
      "type": "object",
      "required": ["conflicts"],
      "properties": {
        "conflicts": {
          "type": "array",
          "items": { "$ref": "#/$defs/strategist_side_conflict" },
          "description": "Same-underlying interactions with analyst recommendations. For position_assessments, populated when an analyst recommendation targets the same underlying. For pending_order_assessments, populated only when order_type is entry_limit or entry_stop_limit and an analyst recommendation targets the same underlying; bracket-leg pending orders (bracket_target, bracket_price_stop, bracket_time_stop, bracket_event_stop) carry empty conflicts because their conflicts are captured by the parent position's assessment."
        }
      }
    },

    "strategist_side_conflict": {
      "type": "object",
      "required": ["with_recommendation_id", "underlying", "conflict_type"],
      "properties": {
        "with_recommendation_id": {
          "type": "string",
          "pattern": "^REC-[0-9]+$",
          "description": "Cross-reference to the analyst recommendation in analyst_section.recommendations[].recommendation."
        },
        "underlying": { "type": "string" },
        "conflict_type": { "$ref": "#/$defs/conflict_type" }
      }
    },

    "analyst_section": {
      "type": "object",
      "required": ["mode"],
      "properties": {
        "mode": {
          "enum": ["normal", "watchlist"],
          "description": "Mirrors the analyst's mode field. Read from the analyst's output."
        },
        "recommendations": {
          "type": "array",
          "items": { "$ref": "#/$defs/wrapped_recommendation" },
          "description": "Present when mode is normal. Order preserved from the analyst's output."
        },
        "watchlist": {
          "type": "array",
          "items": {
            "$ref": "https://alphamind.local/schemas/analyst-output.json#/$defs/watchlist_entry"
          },
          "description": "Present when mode is watchlist. Passed through verbatim from the analyst's output."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "mode": { "const": "normal" } }, "required": ["mode"] },
          "then": { "required": ["recommendations"] }
        },
        {
          "if": { "properties": { "mode": { "const": "watchlist" } }, "required": ["mode"] },
          "then": { "required": ["watchlist"] }
        }
      ]
    },

    "wrapped_recommendation": {
      "type": "object",
      "required": ["recommendation", "pre_processor_annotations"],
      "properties": {
        "recommendation": {
          "$ref": "https://alphamind.local/schemas/analyst-output.json#/$defs/recommendation",
          "description": "Analyst's REC-N record, preserved unmodified."
        },
        "pre_processor_annotations": { "$ref": "#/$defs/analyst_side_annotations" }
      }
    },

    "analyst_side_annotations": {
      "type": "object",
      "required": ["conflicts"],
      "properties": {
        "conflicts": {
          "type": "array",
          "items": { "$ref": "#/$defs/analyst_side_conflict" },
          "description": "Same-underlying interactions with strategist position assessments. Empty when no strategist assessment targets the same underlying."
        }
      }
    },

    "analyst_side_conflict": {
      "type": "object",
      "required": ["underlying", "conflict_type"],
      "properties": {
        "with_assessment_id": {
          "type": "string",
          "pattern": "^SA-[0-9]+$",
          "description": "Cross-reference to a strategist position assessment in strategist_section.position_assessments[].assessment. Present when the conflict is with a position assessment."
        },
        "with_pending_order_assessment_id": {
          "type": "string",
          "pattern": "^SA-ORD-[0-9]+$",
          "description": "Cross-reference to a strategist pending-order assessment in strategist_section.pending_order_assessments[].pending_order_assessment. Present when the conflict is with an unfilled entry order (order_type entry_limit or entry_stop_limit)."
        },
        "underlying": { "type": "string" },
        "conflict_type": { "$ref": "#/$defs/conflict_type" }
      },
      "oneOf": [
        { "required": ["with_assessment_id"] },
        { "required": ["with_pending_order_assessment_id"] }
      ]
    },

    "conflict_type": {
      "enum": [
        "entry_vs_close",
        "entry_vs_add",
        "entry_vs_hold",
        "entry_direction_conflict",
        "entry_vs_pending_maintain",
        "entry_vs_pending_modify",
        "entry_vs_pending_cancel"
      ],
      "description": "Classification of the same-underlying interaction. See proposal-pre-processor.md Wrap pattern and conflicts annotation."
    }
  }
}
```

---

## Notes on cross-field invariants

Some invariants cannot be expressed cleanly in JSON Schema and must be enforced by the validation pipeline:

- **`invocation_id` consistency.** The bundle's `invocation_id` must equal the inner agent records' `invocation_id` values. The pre-processor reads both agents' outputs from the same invocation and copies the ID forward.
- **Mode consistency with halt state.** When `strategist_section.mode = "defensive_posture"`, `analyst_section.mode` is `"watchlist"`; halt is a system-wide state that affects both agents simultaneously. When either is in its halt-mode value, the other's halt-mode value is required.
- **Conflict cross-references resolve in-bundle.** Every `with_recommendation_id` value must match a `recommendation_id` of some `analyst_section.recommendations[].recommendation`. Every `with_assessment_id` must match an `assessment_id` of some `strategist_section.position_assessments[].assessment`. Every `with_pending_order_assessment_id` must match a `pending_order_assessment_id` of some `strategist_section.pending_order_assessments[].pending_order_assessment` whose `order_type` is `entry_limit` or `entry_stop_limit`.
- **Mirror symmetry of conflicts.** A conflict appearing on the analyst side must have a corresponding entry on the strategist side with the same `underlying` and the same `conflict_type`. An analyst-side conflict using `with_assessment_id: "SA-N"` mirrors a conflict on `position_assessments[].pre_processor_annotations.conflicts` with `with_recommendation_id` pointing back; an analyst-side conflict using `with_pending_order_assessment_id: "SA-ORD-N"` mirrors a conflict on `pending_order_assessments[].pre_processor_annotations.conflicts` with `with_recommendation_id` pointing back.
- **Pending-order conflict scope.** Conflict annotations on `pending_order_assessments` are populated only when `order_type` is `entry_limit` or `entry_stop_limit`. Bracket-leg pending orders (`bracket_target`, `bracket_price_stop`, `bracket_time_stop`, `bracket_event_stop`) carry an empty `conflicts` array — their conflicts are captured by the parent position's `position_assessment`.
- **`combined_set_impact.basis.analyst_proposal_ids` consistency.** Every ID in this array must match a `recommendation_id` of some `analyst_section.recommendations[].recommendation`; the array's length need not equal `analyst_section.recommendations` length only when watchlist mode renders both sides empty.
- **`combined_set_impact.basis.strategist_action_ids` consistency.** Every ID must match an `assessment_id` of some position assessment whose `recommended_action ≠ "hold"`. The complement (assessments where `recommended_action = "hold"`) is counted in `strategist_holds_excluded_count`; the sum equals `strategist_section.position_assessments` length.
- **`combined_set_impact.breaches[].rule` correspondence.** Every breach entry's `rule` must match the `rule` of a `per_rule_entry` whose `status` is `FAIL`. Conversely, every `per_rule_entry` with `status: FAIL` must have a corresponding `breaches[]` entry.
- **Contributor IDs valid.** `contributors[].proposal_id` values must match either an analyst recommendation ID or a strategist assessment ID present elsewhere in the bundle.
- **`conviction_distribution.total` equals sum.** The sum of `by_level` counts must equal `total` and equal the length of `analyst_section.recommendations` (or zero in watchlist mode).
- **`book_health_summary.total` consistency.** Equals the length of `strategist_section.position_assessments`. The sum of `by_thesis_status` counts must equal `total`; same for `by_recommended_action`.
- **`remedy_flagged_count` consistency.** Equals the count of `strategist_section.position_assessments[].assessment.remedy_flag` populated entries.
- **`per_rule[].rule` set membership.** Rule identifiers must come from the canonical registry in [`rules-and-limits.md`](../06-risk-guardrails/rules-and-limits.md) and the active profile's rule subset.

---

## Evolution

When new annotation types are added (e.g., pending-order conflict auto-detection, additional aggregate observations), they are added here first. This schema is the authoritative contract; prose updates in [`proposal-pre-processor.md`](proposal-pre-processor.md) follow.
