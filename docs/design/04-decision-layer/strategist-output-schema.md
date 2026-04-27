# Strategist output schema

Formal JSON Schema (Draft 2020-12) for the strategist agent's invocation output. Machine-readable contract for the field list in [strategist.md](strategist.md). The [proposal pre-processor](proposal-pre-processor.md) reads the structured fields; the [portfolio manager](portfolio-manager.md) reads the narrative fields.

## Scope

- **Contract surface:** one strategist invocation produces one output document matching this schema.
- **LLM vs. tool populated:** the strategist produces every field except `exposure_impact` (computed from position data for close/reduce, from the guardrail validation tool for add) and `guardrail_validation_result`, populated by the [guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) at pre-submission check time (see [strategist.md — Pre-submission guardrail validation](strategist.md#pre-submission-guardrail-validation)). Persisted output includes both.
- **Modes:** `normal` or `defensive_posture` (halt-mode — see [strategist.md — Halt mode and defensive-posture behavior](strategist.md#halt-mode-and-defensive-posture-behavior)). The permissible action enum and some required portfolio-level fields differ by mode. Both modes produce position assessments, pending order assessments, and portfolio-level observations; the schema is a strict superset.
- **Feature-flag interaction:** the schema permits options and strategy-action parameters, but the guardrail validation tool returns `FAIL` with reason `feature_disabled` for disabled instrument classes. The strategist's guardrail state header omits options/short sections when disabled, preventing disabled-feature actions upstream.

## Cross-references

Enum values and structural constraints trace back to these sources:

| Field | Source |
|---|---|
| `thesis_status` / `prior_status` enum | [thesis-model.md — thesis status classifications](../05-execution-layer/thesis-model.md#thesis-status-classifications) |
| `sector` enum | [rules-and-limits.md — active_sectors](../06-risk-guardrails/rules-and-limits.md) |
| `recommended_action` enum | [oms-commands.md — command overview](../05-execution-layer/oms-commands.md#command-overview) |
| `close_rationale_type` enum | [oms-commands.md — CLOSE](../05-execution-layer/oms-commands.md#close) |
| `order_type` values | [orders-and-brackets.md — tier 1 atomic orders](../05-execution-layer/orders-and-brackets.md#tier-1--atomic-orders) |
| `guardrail_validation_result` structure | [state-delivery.md — guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) |
| Action-selection signal criteria | [strategist.md — Action decision logic](strategist.md#action-decision-logic) |
| Status classification signal criteria | [strategist.md — Thesis status classification methodology](strategist.md#thesis-status-classification-methodology) |
| `remedy_flag` semantics | [strategist.md — Regime-transition remedy proposals](strategist.md#regime-transition-remedy-proposals) |
| Defensive-posture behavior | [strategist.md — Halt mode and defensive-posture behavior](strategist.md#halt-mode-and-defensive-posture-behavior) |

---

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/strategist-output.json",
  "title": "Strategist invocation output",
  "type": "object",
  "required": [
    "invocation_id",
    "timestamp",
    "mode",
    "position_assessments",
    "pending_order_assessments",
    "portfolio_level_observations"
  ],
  "properties": {
    "invocation_id": {
      "type": "string",
      "description": "Pipeline invocation this output belongs to. Matches the invocation_id in the guardrail state header."
    },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "When the strategist finalized this output (after any validation-driven revisions)."
    },
    "mode": {
      "type": "string",
      "enum": ["normal", "defensive_posture"],
      "description": "Operating mode, read from the guardrail state header. 'defensive_posture' is used when halt mode is active (daily drawdown halt or cumulative drawdown tier 3)."
    },
    "position_assessments": {
      "type": "array",
      "description": "One assessment per open position. Empty array is valid when no positions are open. Presentation order: by status severity (invalidated, at-risk, partially-realized, stale, on-track), then by action urgency, then by portfolio weight descending. Remedy-flagged assessments are ordered first within their status tier.",
      "items": { "$ref": "#/$defs/position_assessment" }
    },
    "pending_order_assessments": {
      "type": "array",
      "description": "One assessment per unfilled order (entry limits, bracket legs). Empty array is valid when no orders are pending. Presentation order: by recommended action (cancel, modify, maintain), then by order age descending.",
      "items": { "$ref": "#/$defs/pending_order_assessment" }
    },
    "portfolio_level_observations": { "$ref": "#/$defs/portfolio_level_observations" }
  },
  "allOf": [
    {
      "if": { "properties": { "mode": { "const": "defensive_posture" } }, "required": ["mode"] },
      "then": {
        "properties": {
          "portfolio_level_observations": {
            "required": ["defensive_posture_summary"]
          },
          "position_assessments": {
            "items": {
              "properties": {
                "recommended_action": {
                  "enum": ["hold", "reduce", "close", "adjust-bracket"]
                }
              }
            }
          }
        }
      }
    }
  ],

  "$defs": {
    "position_assessment": {
      "type": "object",
      "required": [
        "assessment_id",
        "position_id",
        "thesis_id",
        "underlying",
        "sector",
        "thesis_status",
        "recommended_action",
        "status_rationale",
        "action_rationale"
      ],
      "properties": {
        "assessment_id": {
          "type": "string",
          "pattern": "^SA-[0-9]+$",
          "description": "Unique within the invocation. Sequential: SA-1, SA-2, ..."
        },
        "position_id": {
          "type": "string",
          "description": "The position this assessment covers (e.g., POS-NVDA-001)."
        },
        "thesis_id": {
          "type": "string",
          "description": "The thesis record linked to this position."
        },
        "underlying": {
          "type": "string",
          "description": "Root ticker. Used by the pre-processor for same-underlying conflict detection against analyst proposals."
        },
        "sector": {
          "type": "string",
          "enum": ["tech", "semis", "financials", "energy"]
        },
        "thesis_status": {
          "type": "string",
          "enum": ["on-track", "partially-realized", "at-risk", "stale", "invalidated"],
          "description": "The strategist's current-invocation status classification. See strategist.md — Thesis status classification methodology for signal criteria."
        },
        "prior_status": {
          "oneOf": [
            { "type": "string", "enum": ["on-track", "partially-realized", "at-risk", "stale", "invalidated"] },
            { "type": "null" }
          ],
          "description": "The classification from the previous invocation, read from the thesis record. Null on the first invocation after position entry."
        },
        "recommended_action": {
          "type": "string",
          "enum": ["hold", "reduce", "close", "adjust-bracket", "add"],
          "description": "Action enum restricted to hold | reduce | close | adjust-bracket in defensive_posture mode (see mode-level conditional below)."
        },
        "action_parameters": {
          "description": "Required for non-hold actions. Shape depends on action. See discriminated union at $defs/action_parameters.",
          "$ref": "#/$defs/action_parameters"
        },
        "exposure_impact": { "$ref": "#/$defs/exposure_impact" },
        "guardrail_validation_result": { "$ref": "#/$defs/guardrail_validation_result" },
        "remedy_flag": {
          "type": "string",
          "description": "Identifier of a regime-transition or market-movement breach flagged in the strategist's guardrail state header that this assessment addresses (e.g., 'BREACH-1'). Present only when the assessment is a remedy."
        },
        "status_rationale": {
          "type": "string",
          "description": "Signal-level reasoning with source references explaining the thesis_status classification. When status has changed from prior_status, must explain what drove the transition — naming the specific signal or component."
        },
        "action_rationale": {
          "type": "string",
          "description": "Signal-level reasoning explaining what drives the recommended_action. For close with thesis_invalidated close_rationale_type: the specific invalidation reason."
        },
        "reduce_rationale": {
          "type": "string",
          "description": "Required when recommended_action is 'reduce'. Explains why partial rather than full close, and how the reduction quantity maps to the portion of the thesis that weakened or to the breach overage (for remedy reductions)."
        },
        "add_conviction_justification": {
          "type": "string",
          "description": "Required when recommended_action is 'add'. Names the strengthening signal absent at entry. A validating signal ('the thesis is playing out') is not sufficient."
        },
        "adjustment_rationale": {
          "type": "string",
          "description": "Required when recommended_action is 'adjust-bracket'. Pairs the old parameter with the new parameter and names the current signal that makes the original level wrong."
        },
        "remedy_rationale": {
          "type": "string",
          "description": "Required when remedy_flag is present. Explains why this action is the right response to the flagged breach, given the thesis state."
        },
        "cross_position_observations": {
          "type": "string",
          "description": "Portfolio-level dynamics relevant to this position — correlation shifts with other held positions, shared catalyst exposures, engine-originated-closure signal implications for this position's thesis."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "recommended_action": { "const": "reduce" } }, "required": ["recommended_action"] },
          "then": { "required": ["action_parameters", "reduce_rationale", "exposure_impact"] }
        },
        {
          "if": { "properties": { "recommended_action": { "const": "close" } }, "required": ["recommended_action"] },
          "then": { "required": ["action_parameters", "exposure_impact"] }
        },
        {
          "if": { "properties": { "recommended_action": { "const": "adjust-bracket" } }, "required": ["recommended_action"] },
          "then": { "required": ["action_parameters", "adjustment_rationale"] }
        },
        {
          "if": { "properties": { "recommended_action": { "const": "add" } }, "required": ["recommended_action"] },
          "then": { "required": ["action_parameters", "add_conviction_justification", "exposure_impact", "guardrail_validation_result"] }
        },
        {
          "if": { "required": ["remedy_flag"] },
          "then": { "required": ["remedy_rationale"] }
        },
        {
          "if": {
            "properties": { "thesis_status": { "const": "invalidated" } },
            "required": ["thesis_status"]
          },
          "then": {
            "properties": { "recommended_action": { "const": "close" } }
          }
        }
      ]
    },

    "action_parameters": {
      "oneOf": [
        { "$ref": "#/$defs/close_parameters" },
        { "$ref": "#/$defs/reduce_parameters" },
        { "$ref": "#/$defs/adjust_bracket_parameters" },
        { "$ref": "#/$defs/add_parameters" }
      ]
    },

    "close_parameters": {
      "type": "object",
      "required": ["action", "quantity", "order_type", "close_rationale_type"],
      "properties": {
        "action": { "const": "close" },
        "quantity": {
          "oneOf": [
            { "type": "number", "exclusiveMinimum": 0 },
            { "type": "string", "const": "all" }
          ],
          "description": "Number of shares/contracts to close, or 'all' for full close."
        },
        "order_type": { "enum": ["market", "limit"] },
        "limit_price": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Required when order_type is 'limit'."
        },
        "close_rationale_type": {
          "enum": ["thesis_invalidated", "target_reached", "conviction_reduced", "risk_management"],
          "description": "Feeds directly into thesis resolution per oms-commands.md — CLOSE."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "order_type": { "const": "limit" } }, "required": ["order_type"] },
          "then": { "required": ["limit_price"] }
        },
        {
          "if": { "properties": { "close_rationale_type": { "const": "conviction_reduced" } }, "required": ["close_rationale_type"] },
          "then": {
            "properties": { "quantity": { "not": { "const": "all" } } },
            "description": "conviction_reduced is partial-close only per oms-commands.md."
          }
        }
      ]
    },

    "reduce_parameters": {
      "type": "object",
      "required": ["action", "quantity", "order_type"],
      "properties": {
        "action": { "const": "reduce" },
        "quantity": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Number of shares/contracts to reduce. Must be less than the current position quantity — a reduce that closes the full position should use close_parameters with quantity 'all'."
        },
        "order_type": { "enum": ["market", "limit"] },
        "limit_price": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Required when order_type is 'limit'."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "order_type": { "const": "limit" } }, "required": ["order_type"] },
          "then": { "required": ["limit_price"] }
        }
      ]
    },

    "adjust_bracket_parameters": {
      "type": "object",
      "required": ["action"],
      "properties": {
        "action": { "const": "adjust-bracket" },
        "new_stop_level": {
          "type": "object",
          "required": ["trigger_price", "order_type"],
          "properties": {
            "trigger_price": { "type": "number", "exclusiveMinimum": 0 },
            "order_type": { "enum": ["market", "limit", "stop", "stop_limit"] },
            "limit_price": { "type": "number", "exclusiveMinimum": 0 }
          },
          "description": "Replaces the position's current price-based invalidation leg trigger."
        },
        "new_target_level": {
          "type": "object",
          "required": ["price", "order_type"],
          "properties": {
            "price": { "type": "number", "exclusiveMinimum": 0 },
            "order_type": { "enum": ["market", "limit"] }
          },
          "description": "Replaces the position's take-profit target."
        },
        "new_time_expiration": {
          "type": "string",
          "format": "date-time",
          "description": "Replaces the position's time-based invalidation deadline."
        },
        "new_event_invalidation": {
          "type": "object",
          "required": ["event_description"],
          "properties": {
            "event_description": { "type": "string" }
          },
          "description": "Adds or revises an event-based (soft) invalidation leg."
        },
        "thesis_component_updates": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["component_type", "narrative"],
            "properties": {
              "component_type": { "enum": ["entry_rationale", "target_rationale", "invalidation_rationale"] },
              "narrative": { "type": "string" },
              "key_assumptions": { "type": "array", "items": { "type": "string" } }
            }
          },
          "description": "Updated thesis components — per oms-commands.md ADJUST, thesis coverage must remain complete after the adjustment."
        }
      },
      "anyOf": [
        { "required": ["new_stop_level"] },
        { "required": ["new_target_level"] },
        { "required": ["new_time_expiration"] },
        { "required": ["new_event_invalidation"] },
        { "required": ["thesis_component_updates"] }
      ]
    },

    "add_parameters": {
      "type": "object",
      "required": ["action", "additional_quantity", "additional_dollar_value", "entry_order"],
      "properties": {
        "action": { "const": "add" },
        "additional_quantity": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Shares or contracts to add on top of the existing position."
        },
        "additional_dollar_value": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Notional dollar value of the add."
        },
        "entry_order": {
          "type": "object",
          "required": ["type"],
          "properties": {
            "type": { "enum": ["market", "limit", "stop_limit"] },
            "limit_price": { "type": "number", "exclusiveMinimum": 0 },
            "stop_price": { "type": "number", "exclusiveMinimum": 0 }
          }
        },
        "bracket_adjustment": {
          "description": "Optional. When the add changes the position's average cost basis, the bracket may need revision — a new stop level, target level, or time horizon.",
          "$ref": "#/$defs/adjust_bracket_parameters"
        }
      }
    },

    "exposure_impact": {
      "type": "object",
      "required": ["sector_delta_adjusted_change", "net_directional_impact"],
      "properties": {
        "sector_delta_adjusted_change": {
          "type": "number",
          "description": "Expected change in this position's sector delta-adjusted exposure (percentage points of portfolio value). Negative for close/reduce; positive for add."
        },
        "net_directional_impact": {
          "type": "number",
          "description": "Expected change in net long (or net short) exposure. Computed from current position data for close/reduce; populated by the guardrail validation tool for add."
        }
      }
    },

    "guardrail_validation_result": {
      "type": "object",
      "description": "Populated by the guardrail validation tool at pre-submission check time, not by the LLM. Structure mirrors analyst-output-schema.md. Required for add actions; optional for close/reduce whose exposure impact interacts with a flagged constraint.",
      "required": ["overall", "per_rule", "checked_at"],
      "properties": {
        "overall": { "enum": ["PASS", "FAIL"] },
        "per_rule": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["rule", "status", "current", "limit", "projected_after", "headroom_remaining", "unit"],
            "properties": {
              "rule": { "type": "string" },
              "status": { "enum": ["PASS", "FAIL", "WARNING"] },
              "current": { "type": "number" },
              "limit": { "type": "number" },
              "projected_after": { "type": "number" },
              "headroom_remaining": { "type": "number" },
              "unit": { "type": "string" }
            }
          }
        },
        "delta_adjusted_exposure": { "type": "number" },
        "greeks": {
          "type": "object",
          "properties": {
            "delta": { "type": "number" },
            "gamma": { "type": "number" },
            "theta": { "type": "number" },
            "vega": { "type": "number" }
          }
        },
        "cumulative_impact_note": { "type": "string" },
        "checked_at": { "type": "string", "format": "date-time" }
      }
    },

    "pending_order_assessment": {
      "type": "object",
      "required": [
        "pending_order_assessment_id",
        "order_id",
        "position_id",
        "order_type",
        "order_age_hours",
        "fill_probability_assessment",
        "recommended_action",
        "drift_rationale",
        "action_rationale"
      ],
      "properties": {
        "pending_order_assessment_id": {
          "type": "string",
          "pattern": "^SA-ORD-[0-9]+$",
          "description": "Unique within the invocation. Sequential: SA-ORD-1, SA-ORD-2, ..."
        },
        "order_id": { "type": "string" },
        "position_id": {
          "type": "string",
          "description": "The position this order belongs to. For pending entries, this is the pending-state position."
        },
        "order_type": {
          "enum": [
            "entry_limit",
            "entry_stop_limit",
            "bracket_target",
            "bracket_price_stop",
            "bracket_time_stop",
            "bracket_event_stop"
          ]
        },
        "order_age_hours": {
          "type": "number",
          "minimum": 0,
          "description": "Hours elapsed since the order was placed."
        },
        "current_distance_pct": {
          "type": "number",
          "description": "For price-anchored orders, distance between current underlying price and trigger/limit level as a percentage. Omitted for time-based and event-based orders."
        },
        "fill_probability_assessment": {
          "enum": ["likely_soon", "plausible", "unlikely"],
          "description": "Strategist's qualitative read of how the underlying is moving relative to the order level given current signals."
        },
        "recommended_action": { "enum": ["maintain", "modify", "cancel"] },
        "modification_parameters": {
          "type": "object",
          "description": "Required when recommended_action is 'modify'. Lists which fields change and to what.",
          "properties": {
            "new_limit_price": { "type": "number", "exclusiveMinimum": 0 },
            "new_trigger_price": { "type": "number", "exclusiveMinimum": 0 },
            "new_deadline": { "type": "string", "format": "date-time" },
            "new_order_type": { "enum": ["market", "limit", "stop", "stop_limit"] }
          },
          "anyOf": [
            { "required": ["new_limit_price"] },
            { "required": ["new_trigger_price"] },
            { "required": ["new_deadline"] },
            { "required": ["new_order_type"] }
          ]
        },
        "linked_position_assessment_id": {
          "type": "string",
          "pattern": "^SA-[0-9]+$",
          "description": "The SA-N assessment ID for the parent position when the order's disposition should be read alongside the position's status."
        },
        "drift_rationale": {
          "type": "string",
          "description": "How conditions have shifted between when the order was placed and the current invocation."
        },
        "action_rationale": {
          "type": "string",
          "description": "Why maintain / modify / cancel given the drift."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "recommended_action": { "const": "modify" } }, "required": ["recommended_action"] },
          "then": { "required": ["modification_parameters"] }
        }
      ]
    },

    "portfolio_level_observations": {
      "type": "object",
      "required": [
        "aggregate_thesis_health",
        "sector_balance_shifts",
        "thesis_dependency_warnings",
        "capital_allocation_observations"
      ],
      "properties": {
        "aggregate_thesis_health": {
          "type": "string",
          "description": "Distribution of positions across status categories; direction of movement since the prior invocation."
        },
        "sector_balance_shifts": {
          "type": "string",
          "description": "Whether the recommended actions would alter the portfolio's sector exposure profile."
        },
        "thesis_dependency_warnings": {
          "type": "string",
          "description": "Multiple positions sharing catalysts or assumptions whose simultaneous invalidation would produce correlated losses."
        },
        "capital_allocation_observations": {
          "type": "string",
          "description": "Whether the current book is capital-efficient given the aggregate thesis-quality distribution."
        },
        "regime_transition_summary": {
          "type": "object",
          "description": "Present when the guardrail state header flagged regime-transition or market-movement breaches. Cross-references which breaches are addressed by which per-position remedies, and which remain uncured with rationale.",
          "required": ["addressed_breaches", "uncured_breaches"],
          "properties": {
            "addressed_breaches": {
              "type": "array",
              "items": {
                "type": "object",
                "required": ["breach_id", "remedy_assessment_ids"],
                "properties": {
                  "breach_id": { "type": "string" },
                  "remedy_assessment_ids": {
                    "type": "array",
                    "items": { "type": "string", "pattern": "^SA-[0-9]+$" }
                  }
                }
              }
            },
            "uncured_breaches": {
              "type": "array",
              "items": {
                "type": "object",
                "required": ["breach_id", "rationale"],
                "properties": {
                  "breach_id": { "type": "string" },
                  "rationale": {
                    "type": "string",
                    "description": "Why this breach is not addressed in the remedy set (e.g., a hold-with-rationale on a position about to resolve at target)."
                  }
                }
              }
            }
          }
        },
        "defensive_posture_summary": {
          "type": "object",
          "description": "Required when mode is 'defensive_posture'. Explicit orderly-reduction priority list the PM can act on if further risk reduction is needed.",
          "required": ["reduction_priority", "capital_preservation_notes"],
          "properties": {
            "reduction_priority": {
              "type": "array",
              "items": {
                "type": "object",
                "required": ["position_id", "priority_rationale"],
                "properties": {
                  "position_id": { "type": "string" },
                  "priority_rationale": {
                    "type": "string",
                    "description": "Why this position should be reduced ahead of others in the book — weakest thesis, largest exposure contribution, worst risk/reward at current price."
                  }
                }
              }
            },
            "capital_preservation_notes": {
              "type": "string",
              "description": "Book-wide observations bearing on defensive posture — concentrated drawdown contributors, positions whose invalidation levels cluster near the current price, pending orders whose fills would compound drawdown."
            }
          }
        }
      }
    }
  }
}
```

---

## Notes on cross-field invariants

Enforced by the validation pipeline rather than the schema:

- **`action_parameters.action` must match `recommended_action`** — the discriminator on the action-parameters union is denormalized; the pipeline verifies equality.
- **`thesis_status: "invalidated"` requires `recommended_action: "close"`.** Encoded conditionally; any invalidated position not paired with close fails validation.
- **`mode: "defensive_posture"` restricts `recommended_action` to `hold | reduce | close | adjust-bracket`** — enforced as a mode-level conditional in the schema's `allOf` block.
- **`remedy_flag` values must correspond to breach identifiers in the strategist's guardrail state header.** The pre-processor cross-references and produces an annotation on mismatch.
- **Pending-order `linked_position_assessment_id` must match an `assessment_id` in the same document.** Referential check.
- **`position_id` on per-position assessments must match an open position.** Referential check against portfolio state.
- **Action-quantity sanity.** A `reduce` with `quantity` ≥ current position quantity should have been `close` with `quantity: "all"`; `close_parameters quantity: "all"` on a position with no remaining shares is inconsistent. Checked against current portfolio state.

---

## Evolution

New fields are added here first; this schema is the authoritative contract. Prose updates in [strategist.md](strategist.md) follow.
