# OMS command schema

Formal JSON Schema (Draft 2020-12) for the five OMS command types that make up the engine's write API. Machine-readable counterpart to [oms-commands.md](oms-commands.md). Every command submitted to the OMS — regardless of origin — must validate against this schema before processing.

## Scope

- **Contract surface:** one OMS command record. Commands compose into the `commands` array of an envelope. Two envelope schemas consume this one: [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md) (PM-originated, validated by the LLM output validator) and [engine-envelope-schema.md](engine-envelope-schema.md) (continuous-monitor-originated, validated by the OMS command intake layer).
- **LLM vs. infrastructure populated:** `command_id` is assigned by the OMS command intake layer at envelope receipt from the envelope's structural position (see [oms-command-ids.md](../oms-command-ids.md)). The LLM produces envelopes with `command_id` absent; infrastructure fills it. Persisted records always carry `command_id`; the LLM output validator checks PM envelopes with `command_id` omitted per the [PM envelope schema](../04-decision-layer/pm-envelope-schema.md). All other fields come from the command's originator.
- **Command origins:** both PM-originated and engine-originated commands validate against this schema. Per-origin restrictions on the `risk_management_subtype` value for CLOSE commands live in the envelope schemas.
- **Feature-flag interaction:** the schema is a superset — permits OPEN and ADD for options and strategies. The guardrail validation layer rejects commands whose instrument class is disabled by the active [portfolio profile](../06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles), with rejection reason `feature_disabled`.

## Cross-references

Enum values and structural constraints trace back to these authoritative sources:

| Field | Source |
|---|---|
| `command_id` format | [oms-command-ids.md](../oms-command-ids.md) |
| `command_type` enum | [oms-commands.md — command overview](oms-commands.md#command-overview) |
| `instrument.asset_type` variants | [position-model.md](position-model.md) |
| `entry_order.type` values | [orders-and-brackets.md — tier 1 atomic orders](orders-and-brackets.md#tier-1--atomic-orders) |
| `target.target_type` values | [orders-and-brackets.md — P/L-based bracket legs](orders-and-brackets.md#pl-based-bracket-legs-anchor-to-actual-fill-price) |
| `invalidation_legs[].type` values | [orders-and-brackets.md — three invalidation types](orders-and-brackets.md#three-invalidation-types) |
| `close_rationale_type` enum | [oms-commands.md — CLOSE](oms-commands.md#close) |
| Thesis structure | [thesis-model.md](thesis-model.md) |
| Thesis component coverage | [thesis-model.md — mandatory coverage](thesis-model.md#mandatory-coverage) |

---

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/oms-command.json",
  "title": "OMS command",
  "description": "One command submitted to the OMS. Discriminated union on command_type.",
  "type": "object",
  "required": ["command_type"],
  "properties": {
    "command_id": {
      "type": "string",
      "description": "Assigned by the OMS command intake layer, not by the command's originator. Required on persisted records; omitted in LLM-produced envelopes pre-intake. Format per oms-command-ids.md.",
      "oneOf": [
        {
          "pattern": "^inv-[^.]+\\.ENV-(REC|SA|SA-ORD)-[0-9]+\\.[0-9]+\\.[0-9]+$",
          "description": "PM-originated: {invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}"
        },
        {
          "pattern": "^MON\\.[^.]+\\.[0-9]+\\.[0-9]+$",
          "description": "Engine-originated: MON.{monitor_session_id}.{trigger_id}.{command_ordinal}"
        }
      ]
    },
    "command_type": {
      "type": "string",
      "enum": ["open", "close", "adjust", "cancel", "add"],
      "description": "The command's action. See oms-commands.md for per-type semantics."
    }
  },
  "oneOf": [
    { "$ref": "#/$defs/open_command" },
    { "$ref": "#/$defs/close_command" },
    { "$ref": "#/$defs/adjust_command" },
    { "$ref": "#/$defs/cancel_command" },
    { "$ref": "#/$defs/add_command" }
  ],

  "$defs": {

    "open_command": {
      "type": "object",
      "required": [
        "command_type",
        "instrument",
        "entry_order",
        "position_size",
        "target",
        "invalidation_legs",
        "thesis"
      ],
      "properties": {
        "command_type": { "const": "open" },
        "instrument": { "$ref": "#/$defs/instrument" },
        "entry_order": { "$ref": "#/$defs/entry_order" },
        "position_size": { "$ref": "#/$defs/position_size" },
        "target": { "$ref": "#/$defs/target" },
        "invalidation_legs": {
          "type": "array",
          "minItems": 1,
          "description": "At least one leg with is_hard = true is required (price- or time-based). Event legs are soft. See orders-and-brackets.md hard backstop requirement.",
          "items": { "$ref": "#/$defs/invalidation_leg" }
        },
        "thesis": { "$ref": "#/$defs/thesis" }
      }
    },

    "close_command": {
      "type": "object",
      "required": [
        "command_type",
        "position_id",
        "quantity",
        "order_type",
        "close_rationale_type"
      ],
      "properties": {
        "command_type": { "const": "close" },
        "position_id": {
          "type": "string",
          "description": "The position to close. Must reference an open position in portfolio state."
        },
        "quantity": {
          "oneOf": [
            { "type": "number", "exclusiveMinimum": 0 },
            { "type": "string", "const": "all" }
          ],
          "description": "Shares/contracts to close, or 'all' for full close."
        },
        "order_type": {
          "type": "string",
          "enum": ["market", "limit"]
        },
        "limit_price": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Required when order_type is 'limit'."
        },
        "close_rationale_type": {
          "type": "string",
          "enum": ["thesis_invalidated", "target_reached", "conviction_reduced", "risk_management"],
          "description": "Feeds thesis resolution per oms-commands.md. Engine-originated closes always use risk_management."
        },
        "invalidation_reason": {
          "type": "string",
          "description": "Required when close_rationale_type is 'thesis_invalidated'. The specific condition or evidence that proves the thesis wrong."
        },
        "risk_management_subtype": {
          "type": "string",
          "enum": ["pm_directed", "engine_guardrail"],
          "description": "Required when close_rationale_type is 'risk_management'. Distinguishes PM-originated risk management closes from engine-originated guardrail-triggered closes in the feedback loop."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "order_type": { "const": "limit" } }, "required": ["order_type"] },
          "then": { "required": ["limit_price"] }
        },
        {
          "if": { "properties": { "close_rationale_type": { "const": "thesis_invalidated" } }, "required": ["close_rationale_type"] },
          "then": { "required": ["invalidation_reason"] }
        },
        {
          "if": { "properties": { "close_rationale_type": { "const": "risk_management" } }, "required": ["close_rationale_type"] },
          "then": { "required": ["risk_management_subtype"] }
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

    "adjust_command": {
      "type": "object",
      "required": ["command_type", "position_id", "adjustment_rationale"],
      "properties": {
        "command_type": { "const": "adjust" },
        "position_id": {
          "type": "string",
          "description": "The position to adjust. Must reference an open position in portfolio state."
        },
        "new_stop_level": {
          "type": "object",
          "required": ["trigger_price", "order_type"],
          "properties": {
            "trigger_price": { "type": "number", "exclusiveMinimum": 0 },
            "order_type": { "enum": ["market", "limit", "stop", "stop_limit"] },
            "limit_price": { "type": "number", "exclusiveMinimum": 0 }
          },
          "description": "Replaces the position's current price-based invalidation leg trigger. For options and strategy positions, trigger_price is evaluated against the underlying's price per orders-and-brackets.md."
        },
        "new_target_level": {
          "type": "object",
          "required": ["order_type"],
          "properties": {
            "target_type": {
              "type": "string",
              "enum": ["absolute_price", "pl_percentage", "pl_dollar"]
            },
            "price": { "type": "number", "exclusiveMinimum": 0 },
            "pl_percentage": { "type": "number" },
            "pl_dollar": { "type": "number" },
            "order_type": { "enum": ["market", "limit"] }
          },
          "description": "Replaces the position's take-profit target. P/L-based targets re-anchor to the actual fill price at bracket activation per orders-and-brackets.md."
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
          "description": "Updated thesis components. Every bracket leg must still have corresponding thesis coverage after the adjustment per thesis-model.md mandatory coverage.",
          "items": { "$ref": "#/$defs/thesis_component" }
        },
        "adjustment_rationale": {
          "type": "string",
          "description": "Why the bracket is being modified. Logged as a deviation in the activity log per oms-commands.md."
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

    "cancel_command": {
      "type": "object",
      "required": ["command_type", "order_id", "cancel_reason"],
      "properties": {
        "command_type": { "const": "cancel" },
        "order_id": {
          "type": "string",
          "description": "The specific pending order to cancel. Must reference an order in a cancellable state (pending, not already filled or expired)."
        },
        "cancel_reason": {
          "type": "string",
          "description": "Why the order is being withdrawn — thesis no longer valid, conditions changed, entry price no longer attractive, etc."
        }
      }
    },

    "add_command": {
      "type": "object",
      "required": [
        "command_type",
        "position_id",
        "additional_quantity",
        "additional_dollar_value",
        "entry_order",
        "thesis_addition_component"
      ],
      "properties": {
        "command_type": { "const": "add" },
        "position_id": {
          "type": "string",
          "description": "The position to add to. Must reference an open position in portfolio state."
        },
        "additional_quantity": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Shares/contracts to add on top of the existing position."
        },
        "additional_dollar_value": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Notional dollar value of the add. Required for the guardrail layer to validate against dollar-based limits."
        },
        "entry_order": { "$ref": "#/$defs/entry_order" },
        "thesis_addition_component": {
          "allOf": [
            { "$ref": "#/$defs/thesis_component" },
            {
              "properties": {
                "component_type": { "const": "entry_rationale" }
              }
            }
          ],
          "description": "A new entry rationale component explaining why conviction increased. Appended to the existing thesis; does not replace prior components."
        },
        "bracket_adjustment": {
          "type": "object",
          "description": "Optional. When the add changes the position's average cost basis, the bracket may need revision — a new stop level, target, or time horizon. Structurally identical to adjust_command fields.",
          "properties": {
            "new_stop_level": {
              "type": "object",
              "required": ["trigger_price", "order_type"],
              "properties": {
                "trigger_price": { "type": "number", "exclusiveMinimum": 0 },
                "order_type": { "enum": ["market", "limit", "stop", "stop_limit"] },
                "limit_price": { "type": "number", "exclusiveMinimum": 0 }
              }
            },
            "new_target_level": {
              "type": "object",
              "required": ["order_type"],
              "properties": {
                "target_type": { "type": "string", "enum": ["absolute_price", "pl_percentage", "pl_dollar"] },
                "price": { "type": "number", "exclusiveMinimum": 0 },
                "pl_percentage": { "type": "number" },
                "pl_dollar": { "type": "number" },
                "order_type": { "enum": ["market", "limit"] }
              }
            },
            "new_time_expiration": { "type": "string", "format": "date-time" }
          },
          "anyOf": [
            { "required": ["new_stop_level"] },
            { "required": ["new_target_level"] },
            { "required": ["new_time_expiration"] }
          ]
        }
      }
    },

    "instrument": {
      "oneOf": [
        { "$ref": "#/$defs/instrument_equity" },
        { "$ref": "#/$defs/instrument_option" },
        { "$ref": "#/$defs/instrument_strategy" }
      ]
    },
    "instrument_equity": {
      "type": "object",
      "required": ["asset_type", "ticker", "direction"],
      "properties": {
        "asset_type": { "const": "equity" },
        "ticker": { "type": "string" },
        "direction": { "enum": ["long", "short"] }
      }
    },
    "instrument_option": {
      "type": "object",
      "required": ["asset_type", "underlying", "strike", "expiration", "contract_type", "direction"],
      "properties": {
        "asset_type": { "const": "option" },
        "underlying": { "type": "string" },
        "strike": { "type": "number", "exclusiveMinimum": 0 },
        "expiration": { "type": "string", "format": "date" },
        "contract_type": { "enum": ["call", "put"] },
        "direction": { "enum": ["long", "short"] }
      }
    },
    "instrument_strategy": {
      "type": "object",
      "required": ["asset_type", "strategy_type", "underlying", "legs"],
      "properties": {
        "asset_type": { "const": "strategy" },
        "strategy_type": {
          "enum": [
            "vertical_spread",
            "calendar_spread",
            "straddle",
            "strangle",
            "iron_condor",
            "custom"
          ]
        },
        "underlying": { "type": "string" },
        "legs": {
          "type": "array",
          "minItems": 2,
          "items": {
            "type": "object",
            "required": ["strike", "expiration", "contract_type", "direction", "quantity_ratio"],
            "properties": {
              "strike": { "type": "number", "exclusiveMinimum": 0 },
              "expiration": { "type": "string", "format": "date" },
              "contract_type": { "enum": ["call", "put"] },
              "direction": { "enum": ["long", "short"] },
              "quantity_ratio": { "type": "integer", "minimum": 1 }
            }
          }
        }
      }
    },

    "entry_order": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": { "enum": ["market", "limit", "stop_limit"] },
        "limit_price": { "type": "number", "exclusiveMinimum": 0 },
        "stop_price": { "type": "number", "exclusiveMinimum": 0 }
      },
      "allOf": [
        {
          "if": { "properties": { "type": { "const": "limit" } }, "required": ["type"] },
          "then": { "required": ["limit_price"] }
        },
        {
          "if": { "properties": { "type": { "const": "stop_limit" } }, "required": ["type"] },
          "then": { "required": ["limit_price", "stop_price"] }
        }
      ]
    },

    "position_size": {
      "type": "object",
      "required": ["quantity", "dollar_value"],
      "properties": {
        "quantity": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Shares for equity, contracts for options, strategy units for strategies. Fractional shares permitted when the active profile sets fractional_shares_required = true."
        },
        "dollar_value": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Notional dollar value. Required for the guardrail layer to validate against dollar-based limits."
        },
        "premium_at_risk": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Required for defined-risk options/strategies. Sizing bands apply to this value for defined-risk instruments (see analyst.md conviction scale)."
        }
      }
    },

    "target": {
      "type": "object",
      "required": ["target_type", "order_type"],
      "properties": {
        "target_type": {
          "enum": ["absolute_price", "pl_percentage", "pl_dollar"],
          "description": "Anchor type. pl_percentage and pl_dollar targets are re-anchored to the actual fill price at bracket activation."
        },
        "price": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Absolute target price. For pl_percentage/pl_dollar, this is the price equivalent computed from the planned entry price — re-anchored at fill time."
        },
        "pl_percentage": {
          "type": "number",
          "description": "Required when target_type is pl_percentage. The percentage value (e.g., 80 for +80% on premium)."
        },
        "pl_dollar": {
          "type": "number",
          "description": "Required when target_type is pl_dollar."
        },
        "order_type": {
          "enum": ["market", "limit"],
          "description": "The take-profit leg's order type."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "target_type": { "const": "absolute_price" } }, "required": ["target_type"] },
          "then": { "required": ["price"] }
        },
        {
          "if": { "properties": { "target_type": { "const": "pl_percentage" } }, "required": ["target_type"] },
          "then": { "required": ["pl_percentage", "price"] }
        },
        {
          "if": { "properties": { "target_type": { "const": "pl_dollar" } }, "required": ["target_type"] },
          "then": { "required": ["pl_dollar", "price"] }
        }
      ]
    },

    "invalidation_leg": {
      "type": "object",
      "required": ["type", "is_hard", "condition"],
      "properties": {
        "type": { "enum": ["price", "time", "event"] },
        "is_hard": {
          "type": "boolean",
          "description": "True for price and time legs (engine-enforced). False for event legs (pipeline-evaluated, PM-acted). At least one leg per command must be true."
        },
        "condition": {
          "oneOf": [
            {
              "type": "object",
              "required": ["underlying_trigger", "comparator", "trigger_price"],
              "properties": {
                "underlying_trigger": {
                  "type": "string",
                  "description": "Ticker whose price the stop trigger evaluates. For options/strategies, this is the underlying equity — stops trigger on the underlying, not the option price (see orders-and-brackets.md)."
                },
                "comparator": { "enum": ["<=", ">=", "<", ">"] },
                "trigger_price": { "type": "number", "exclusiveMinimum": 0 }
              }
            },
            {
              "type": "object",
              "required": ["deadline"],
              "properties": {
                "deadline": { "type": "string", "format": "date-time" }
              }
            },
            {
              "type": "object",
              "required": ["event_description"],
              "properties": {
                "event_description": {
                  "type": "string",
                  "description": "Qualitative condition the analysis pipeline will evaluate (e.g., 'MSFT guides AI capex lower than consensus')."
                }
              }
            }
          ]
        },
        "order_parameters": {
          "type": "object",
          "description": "Required for type = price and type = time. Omitted for type = event (soft leg, no mechanical enforcement).",
          "required": ["order_type"],
          "properties": {
            "order_type": { "enum": ["market", "limit", "stop", "stop_limit"] },
            "limit_price": { "type": "number", "exclusiveMinimum": 0 }
          }
        }
      },
      "allOf": [
        {
          "if": { "properties": { "type": { "const": "price" } }, "required": ["type"] },
          "then": { "required": ["order_parameters"], "properties": { "is_hard": { "const": true } } }
        },
        {
          "if": { "properties": { "type": { "const": "time" } }, "required": ["type"] },
          "then": { "required": ["order_parameters"], "properties": { "is_hard": { "const": true } } }
        },
        {
          "if": { "properties": { "type": { "const": "event" } }, "required": ["type"] },
          "then": { "properties": { "is_hard": { "const": false } } }
        }
      ]
    },

    "thesis": {
      "type": "object",
      "required": ["summary", "components"],
      "properties": {
        "summary": {
          "type": "string",
          "description": "The overall trade rationale in one or two sentences. Self-contained — a reader who sees only the summary should understand the core bet."
        },
        "components": {
          "type": "array",
          "minItems": 1,
          "description": "Every bracket leg must have a corresponding component per thesis-model.md mandatory coverage. The engine rejects a thesis that does not cover every leg.",
          "items": { "$ref": "#/$defs/thesis_component" }
        }
      }
    },

    "thesis_component": {
      "type": "object",
      "required": ["component_type", "linked_leg", "instrument_reference", "narrative", "key_assumptions"],
      "properties": {
        "component_type": {
          "enum": ["entry_rationale", "target_rationale", "invalidation_rationale"]
        },
        "linked_leg": {
          "type": "string",
          "description": "Which specific order or bracket leg this component addresses. Values: 'entry', 'target', 'price_stop', 'time_expiration', 'event_invalidation', or 'strategy_leg_<n>' for multi-leg strategies."
        },
        "instrument_reference": {
          "type": "string",
          "description": "Ticker or multi-leg strategy leg identifier this component pertains to."
        },
        "narrative": {
          "type": "string",
          "description": "The specific reasoning for this component — which signals support it, what the expected causal chain is, why this threshold was chosen."
        },
        "key_assumptions": {
          "type": "array",
          "description": "Discrete falsifiable claims this component depends on, expressed as testable statements.",
          "items": { "type": "string" }
        }
      }
    }
  }
}
```

---

## Notes on cross-field invariants

Invariants enforced by the validation pipeline, OMS command intake layer, or guardrail layer (not expressible in JSON Schema):

- **Every bracket leg must have a corresponding thesis component** (mandatory coverage per [thesis-model.md](thesis-model.md#mandatory-coverage)). For OPEN: each `invalidation_legs[]` entry must have a matching `invalidation_rationale` component with a `linked_leg` pointing to it, plus at least one `entry_rationale` component and one `target_rationale` component. For ADJUST: after adjustment, every remaining bracket leg must still have a thesis component — unchanged or updated via `thesis_component_updates`. Enforced at command receipt.

- **`position_id` must reference an open position** in portfolio state at submission time. Applies to `close_command`, `adjust_command`, `add_command`.

- **`order_id` must reference an order in a cancellable state** (pending, not filled or expired). Applies to `cancel_command`.

- **Per-origin `risk_management_subtype` on CLOSE commands.** Engine-originated uses `engine_guardrail` per [engine-envelope-schema.md](engine-envelope-schema.md); PM-originated uses `pm_directed` per [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md).

- **`instrument.asset_type` must be permitted by the active portfolio profile.** Guardrail validation returns `feature_disabled` for options or shorts on the primary portfolio.

- **`command_id` format depends on origin.** PM-originated and engine-originated use distinct patterns ([oms-command-ids.md](../oms-command-ids.md)); the envelope's source_provenance determines which applies.

- **`attempt_seq` in PM-originated command IDs corresponds to post-rejection modification count.** Enforced at ID derivation per [oms-command-ids.md §attempt_seq computation](../oms-command-ids.md#attempt_seq-computation).

- **Thesis component `linked_leg` values must reference actual legs in the command's bracket.** `linked_leg: price_stop` requires a price-type `invalidation_legs[]` entry; `time_expiration` a time-type entry; `event_invalidation` an event-type entry; `strategy_leg_<n>` requires the `n`-th leg to exist on a strategy instrument.

---

## Evolution

New command types, required parameters, or close rationale categories land here first; prose in [oms-commands.md](oms-commands.md) follows. The discriminated-union shape (one `command_type` per variant with a `oneOf` over variants) is load-bearing — downstream consumers branch on `command_type` and every variant carries its full required set. Adding a command type requires a new `$defs` entry and extending the top-level `oneOf`.

---

## Cross-references

- OMS command prose contract: [oms-commands.md](oms-commands.md)
- OMS command ID discipline: [../oms-command-ids.md](../oms-command-ids.md)
- PM envelope schema (wraps PM-originated commands): [../04-decision-layer/pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md)
- Engine envelope schema (wraps continuous-monitor-originated commands): [engine-envelope-schema.md](engine-envelope-schema.md)
- Analyst output schema (whose instrument / entry_order / target / invalidation_leg definitions this schema deliberately mirrors): [../04-decision-layer/analyst-output-schema.md](../04-decision-layer/analyst-output-schema.md)
- Thesis model: [thesis-model.md](thesis-model.md)
- Orders and brackets: [orders-and-brackets.md](orders-and-brackets.md)
- Position model: [position-model.md](position-model.md)
- Portfolio profiles and feature flags: [../06-risk-guardrails/rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)
- LLM output validation mechanism: [../testing/llm-output-validation.md](../testing/llm-output-validation.md)
