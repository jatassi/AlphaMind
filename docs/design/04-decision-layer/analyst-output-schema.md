# Analyst output schema

Formal JSON Schema (Draft 2020-12) for the analyst agent's invocation output. This is the machine-readable contract that corresponds to the prose field list in [analyst.md](analyst.md). The [proposal pre-processor](proposal-pre-processor.md) reads the structured fields; the [portfolio manager](portfolio-manager.md) reads the narrative fields.

## Scope

- **Contract surface:** one analyst invocation produces one output document matching this schema.
- **LLM vs. tool populated:** the analyst agent produces every field except `position_size.delta_adjusted_exposure` and `guardrail_validation_result`, which are populated by the [guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) at pre-submission check time (see [analyst.md](analyst.md#pre-submission-guardrail-validation)). The final persisted output includes both.
- **Modes:** `normal` (standard proposal generation) or `watchlist` (halt-mode operation — see [state-delivery.md](../06-risk-guardrails/state-delivery.md#analyst--watchlist-mode)). Exactly one of `recommendations` or `watchlist` is populated, keyed by `mode`.
- **Feature-flag interaction:** the schema is superset — it permits options and strategy proposals — but the guardrail validation tool returns `FAIL` with reason `feature_disabled` when those instruments are disabled by the active [portfolio profile](../06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles). The analyst's guardrail state header omits options/short sections when disabled, naturally preventing disabled-feature proposals upstream.

## Cross-references

Enum values and structural constraints trace back to these authoritative sources:

| Field | Source |
|---|---|
| `sector` enum | [rules-and-limits.md — active_sectors](../06-risk-guardrails/rules-and-limits.md) |
| `conviction_level` bands | [analyst.md — conviction scale](analyst.md#conviction-scale) |
| `instrument.asset_type` variants | [position-model.md](../05-execution-layer/position-model.md) |
| `entry_order.type` values | [orders-and-brackets.md — tier 1 atomic orders](../05-execution-layer/orders-and-brackets.md#tier-1--atomic-orders) |
| `target.target_type` values | [orders-and-brackets.md — P/L-based bracket legs](../05-execution-layer/orders-and-brackets.md#pl-based-bracket-legs-anchor-to-actual-fill-price) |
| `invalidation_legs[].type` values | [orders-and-brackets.md — three invalidation types](../05-execution-layer/orders-and-brackets.md#three-invalidation-types) |
| `entry_window` structure | [analyst.md — entry window](analyst.md#entry-window) |
| `guardrail_validation_result` structure | [state-delivery.md — guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) |
| `watchlist` entry fields | [state-delivery.md — watchlist mode](../06-risk-guardrails/state-delivery.md#analyst--watchlist-mode) |

---

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/analyst-output.json",
  "title": "Analyst invocation output",
  "type": "object",
  "required": ["invocation_id", "timestamp", "mode"],
  "properties": {
    "invocation_id": {
      "type": "string",
      "description": "Pipeline invocation this output belongs to. Matches the invocation_id in the guardrail state header."
    },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "When the analyst finalized this output (after any validation-driven revisions)."
    },
    "mode": {
      "type": "string",
      "enum": ["normal", "watchlist"],
      "description": "Operating mode, read from the guardrail state. 'watchlist' is used when halt mode is active."
    },
    "recommendations": {
      "type": "array",
      "description": "Present when mode = normal. Empty array is valid on quiet days with no actionable setups.",
      "items": { "$ref": "#/$defs/recommendation" }
    },
    "watchlist": {
      "type": "array",
      "description": "Present when mode = watchlist. Lighter-weight entries for post-halt review; no sizing, entry, or bracket detail.",
      "items": { "$ref": "#/$defs/watchlist_entry" }
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
  ],

  "$defs": {
    "recommendation": {
      "type": "object",
      "required": [
        "recommendation_id",
        "instrument",
        "underlying",
        "sector",
        "conviction_level",
        "entry_order",
        "position_size",
        "target",
        "invalidation_legs",
        "time_expectation_hours",
        "guardrail_validation_result",
        "thesis_narrative",
        "target_rationale",
        "invalidation_rationale",
        "position_size_rationale",
        "counterarguments_acknowledged"
      ],
      "properties": {
        "recommendation_id": {
          "type": "string",
          "pattern": "^REC-[0-9]+$",
          "description": "Unique within the invocation. Sequential: REC-1, REC-2, ..."
        },
        "instrument": { "$ref": "#/$defs/instrument" },
        "underlying": {
          "type": "string",
          "description": "Root ticker. Equals instrument.ticker for equity; equals instrument.underlying for options/strategies. Used by the pre-processor for same-underlying conflict detection and sector classification."
        },
        "sector": {
          "type": "string",
          "enum": ["tech", "semis", "financials", "energy"],
          "description": "Must belong to the active portfolio profile's active_sectors."
        },
        "conviction_level": {
          "type": "integer",
          "minimum": 1,
          "maximum": 5,
          "description": "Speculative (1) through maximum (5). See analyst.md conviction scale — sizing must fall within the corresponding advisory band unless position_size_rationale explains the deviation."
        },
        "entry_order": { "$ref": "#/$defs/entry_order" },
        "position_size": { "$ref": "#/$defs/position_size" },
        "target": { "$ref": "#/$defs/target" },
        "invalidation_legs": {
          "type": "array",
          "minItems": 1,
          "description": "At least one leg with is_hard = true is required (price- or time-based). Event legs are soft. See orders-and-brackets.md hard backstop requirement.",
          "items": { "$ref": "#/$defs/invalidation_leg" }
        },
        "entry_window": {
          "$ref": "#/$defs/entry_window",
          "description": "Optional. Include when the setup has a meaningful time constraint (catalyst timestamp, theta decay, information edge leakage)."
        },
        "time_expectation_hours": {
          "type": "number",
          "exclusiveMinimum": 0,
          "maximum": 72,
          "description": "Expected resolution horizon. AlphaMind targets 4–72 hours."
        },
        "guardrail_validation_result": { "$ref": "#/$defs/guardrail_validation_result" },
        "thesis_narrative": {
          "type": "string",
          "description": "Causal chain from signal(s) to expected price movement. Must embed source references (e.g., [SA-TECH-3], [QR-4], [AR-2], [CR-1]) that correspond to the synthesizer brief's typed references."
        },
        "target_rationale": {
          "type": "string",
          "description": "Why this exit level and what catalyst should drive price there."
        },
        "invalidation_rationale": {
          "type": "array",
          "minItems": 1,
          "description": "One entry per leg in invalidation_legs. leg_id values must match.",
          "items": {
            "type": "object",
            "required": ["leg_id", "rationale"],
            "properties": {
              "leg_id": { "type": "string" },
              "rationale": { "type": "string" }
            }
          }
        },
        "position_size_rationale": {
          "type": "string",
          "description": "How sizing maps to conviction level and the advisory band, accounting for the instrument's risk profile (defined-risk vs. open-ended — see analyst.md conviction scale)."
        },
        "entry_window_rationale": {
          "type": "string",
          "description": "Required when entry_window is present. Explains why the window exists and what happens after it closes."
        },
        "counterarguments_acknowledged": {
          "type": "string",
          "description": "Strongest case against the trade and why the analyst proceeds despite it. Consumed by the PM's counterargument-consideration thesis-quality criterion."
        }
      },
      "allOf": [
        {
          "if": { "required": ["entry_window"] },
          "then": { "required": ["entry_window_rationale"] }
        }
      ]
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
              "quantity_ratio": {
                "type": "integer",
                "minimum": 1,
                "description": "Ratio of this leg per one strategy unit. Absolute contract count = position_size.quantity × quantity_ratio."
              }
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
      "required": ["quantity", "dollar_value", "pct_of_portfolio"],
      "properties": {
        "quantity": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Shares for equity, contracts for options, strategy units for strategies. Fractional shares permitted when the active profile sets fractional_shares_required = true."
        },
        "dollar_value": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Notional dollar value. For defined-risk options (long options, debit spreads), also populate premium_at_risk."
        },
        "pct_of_portfolio": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Position size as a percentage of total portfolio value (cash + positions). Should fall within the conviction-level advisory band unless rationale explains otherwise."
        },
        "premium_at_risk": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Required for defined-risk options/strategies. The sizing band applies to this value for defined-risk instruments (see analyst.md conviction scale — sizing bands express capital at risk, not notional)."
        },
        "delta_adjusted_exposure": {
          "type": "number",
          "description": "Populated by the guardrail validation tool, not the LLM. Equals dollar_value for equity; equals contract_count × multiplier × |delta| × underlying_price for options/strategies."
        }
      }
    },

    "target": {
      "type": "object",
      "required": ["target_type", "price", "dollar_pl_target"],
      "properties": {
        "target_type": {
          "enum": ["absolute_price", "pl_percentage", "pl_dollar"],
          "description": "Anchor type. pl_percentage and pl_dollar targets are re-anchored to the actual fill price at bracket activation."
        },
        "price": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Absolute target price. For pl_percentage/pl_dollar, this is the price equivalent computed from the planned entry price."
        },
        "dollar_pl_target": {
          "type": "number",
          "description": "Expected dollar P/L when the target is reached. Used by the pre-processor and PM for cross-proposal ranking and capital planning."
        },
        "pl_percentage": {
          "type": "number",
          "description": "Required when target_type = pl_percentage. The percentage value (e.g., 80 for +80% on premium)."
        },
        "pl_dollar": {
          "type": "number",
          "description": "Required when target_type = pl_dollar."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "target_type": { "const": "pl_percentage" } }, "required": ["target_type"] },
          "then": { "required": ["pl_percentage"] }
        },
        {
          "if": { "properties": { "target_type": { "const": "pl_dollar" } }, "required": ["target_type"] },
          "then": { "required": ["pl_dollar"] }
        }
      ]
    },

    "invalidation_leg": {
      "type": "object",
      "required": ["leg_id", "type", "is_hard", "condition"],
      "properties": {
        "leg_id": {
          "type": "string",
          "pattern": "^INV-[0-9]+$",
          "description": "Unique within the recommendation. Referenced by invalidation_rationale entries."
        },
        "type": { "enum": ["price", "time", "event"] },
        "is_hard": {
          "type": "boolean",
          "description": "True for price and time legs (engine-enforced). False for event legs (pipeline-evaluated, PM-acted). At least one leg per recommendation must be true."
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

    "entry_window": {
      "type": "object",
      "required": ["deadline", "decay_type", "rationale"],
      "properties": {
        "deadline": { "type": "string", "format": "date-time" },
        "decay_type": {
          "enum": ["binary", "gradual"],
          "description": "binary: setup exists or doesn't (hard-catalyst trades). gradual: edge erodes progressively (technicals, information edges)."
        },
        "rationale": {
          "type": "string",
          "description": "Why the window exists and what happens after it closes."
        }
      }
    },

    "guardrail_validation_result": {
      "type": "object",
      "description": "Populated by the guardrail validation tool at pre-submission check time, not by the LLM.",
      "required": ["overall", "per_rule", "checked_at"],
      "properties": {
        "overall": { "enum": ["PASS", "FAIL"] },
        "per_rule": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["rule", "status", "current", "limit", "projected_after", "headroom_remaining", "unit"],
            "properties": {
              "rule": {
                "type": "string",
                "description": "Rule identifier (e.g., 'sector_concentration', 'per_position_max_size', 'net_long_exposure', 'gross_exposure', 'daily_drawdown', 'correlation', 'options_delta_exposure')."
              },
              "status": { "enum": ["PASS", "FAIL", "WARNING"] },
              "current": { "type": "number" },
              "limit": { "type": "number" },
              "projected_after": { "type": "number" },
              "headroom_remaining": { "type": "number" },
              "unit": { "type": "string" }
            }
          }
        },
        "delta_adjusted_exposure": {
          "type": "number",
          "description": "Computed delta-adjusted exposure for this proposal. Mirrored into position_size.delta_adjusted_exposure."
        },
        "greeks": {
          "type": "object",
          "description": "Options and strategies only. Computed via the guardrail-evaluation library (Black-Scholes with the conservative delta buffer); see ../06-risk-guardrails/guardrail-evaluation.md.",
          "properties": {
            "delta": { "type": "number" },
            "gamma": { "type": "number" },
            "theta": { "type": "number" },
            "vega": { "type": "number" }
          }
        },
        "cumulative_impact_note": {
          "type": "string",
          "description": "Indicates which proposal this is within the invocation and confirms that prior proposals' projected impact is included in headroom."
        },
        "checked_at": { "type": "string", "format": "date-time" }
      }
    },

    "watchlist_entry": {
      "type": "object",
      "required": ["ticker", "sector", "thesis_summary", "estimated_conviction"],
      "properties": {
        "ticker": { "type": "string" },
        "sector": { "type": "string", "enum": ["tech", "semis", "financials", "energy"] },
        "thesis_summary": {
          "type": "string",
          "description": "One or two sentences — the core bet and the catalyst to watch for."
        },
        "estimated_conviction": { "type": "integer", "minimum": 1, "maximum": 5 },
        "source_references": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Synthesizer brief reference IDs (e.g., [SA-TECH-3]) that motivated this watchlist entry."
        }
      }
    }
  }
}
```

---

## Notes on cross-field invariants

Some invariants cannot be expressed cleanly in JSON Schema and must be enforced by the validation pipeline:

- **`invalidation_rationale[].leg_id` must match an existing `invalidation_legs[].leg_id`** — the schema requires both arrays and enforces that each leg has a rationale entry via array length conventions, but the ID correspondence is a referential check.
- **At least one `invalidation_legs[]` entry must have `is_hard: true`** — encoded conditionally by type (price/time force hard, event forces soft), so any valid recommendation with at least one price or time leg satisfies it.
- **`underlying` must equal `instrument.ticker` for equity, or `instrument.underlying` for options/strategies** — denormalized for pre-processor convenience; the validation pipeline should verify equality.
- **`position_size.pct_of_portfolio` should fall within the advisory band for the declared `conviction_level`** unless `position_size_rationale` explains the deviation. Not strictly enforced — see [analyst.md — conviction scale](analyst.md#conviction-scale) ("Bands are advisory, not enforced").
- **`sector` must be in the active portfolio profile's `active_sectors`** — enforced by the guardrail validation tool (returns `FAIL` with reason `feature_disabled` for disabled sectors).
- **`instrument.asset_type` must be permitted by the active profile** — enforced by the guardrail validation tool (`FAIL` with reason `feature_disabled` for options or shorts on the primary portfolio).

---

## Evolution

When new fields are added to the analyst's output (e.g., opportunity-ranking fields from the pending [Phase 2 item](../../project-tracker.md#analyst-agent)), they are added here first. This schema is the authoritative contract; prose updates in [analyst.md](analyst.md) follow.
