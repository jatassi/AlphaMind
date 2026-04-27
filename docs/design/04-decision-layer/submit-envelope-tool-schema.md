# `submit_envelope` tool schema

Formal JSON Schema (Draft 2020-12) for the `submit_envelope` tool — the PM's transport for handing one finalized envelope to the engine and receiving the per-command result list. Machine-readable contract for [portfolio-manager.md — Synchronous command feedback](portfolio-manager.md#synchronous-command-feedback). The PM, running as a Claude tool-use loop, receives engine acknowledgments and rejection payloads through this tool's response.

## Scope

- **Contract surface:** one tool call. Input is one PM envelope; output is one structured response with one `submission_result` per embedded command. The envelope is the unit of submission per [pm-envelope-schema.md §Scope](pm-envelope-schema.md); the engine drives per-command sequencing and cumulative-state accounting per [oms-commands.md §Sequencing within an invocation](../05-execution-layer/oms-commands.md#sequencing-within-an-invocation).
- **Producer:** the engine's submission tool, exposed to the PM via the agent SDK ([llm-integration.md](../architecture/llm-integration.md)). Not an LLM-produced contract — the schema constrains the deterministic engine response.
- **Consumer:** the PM agent. Reads `status` per command; on `rejected`, appends a `modification_record` with `phase: post_rejection` / `adjustment_category: guardrail_rejection_response` / `triggering_rule` to the envelope per [pm-envelope-schema.md §modification_record](pm-envelope-schema.md), revises the embedded command, and re-invokes `submit_envelope` with the revised envelope.
- **Synchronous semantics:** the tool returns within the same invocation per [portfolio-manager.md §Synchronous command feedback](portfolio-manager.md#synchronous-command-feedback). No async polling, no out-of-band callbacks. In-process pipeline and OMS guarantee call-or-raise semantics per [oms-command-ids.md §Why not dedup](../oms-command-ids.md#why-not-dedup).
- **Origin scope:** PM-originated envelopes only. The continuous monitor's protective CLOSE submission path is specified in [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md) and [oms-commands.md §Command origins](../05-execution-layer/oms-commands.md#command-origins).

## Cross-references

Enum values, payload fields, and structural constraints trace back to these sources:

| Field | Source |
|---|---|
| Input envelope shape | [pm-envelope-schema.md](pm-envelope-schema.md) |
| Embedded command shape | [../05-execution-layer/oms-command-schema.md](../05-execution-layer/oms-command-schema.md) |
| `command_id` format on accepted results | [../oms-command-ids.md](../oms-command-ids.md) |
| `rejection_payload` field set | [../06-risk-guardrails/breach-behavior.md §Hard rejection semantics](../06-risk-guardrails/breach-behavior.md#hard-rejection-semantics) |
| Per-rule field naming (`rule`, `current`, `limit`, `unit`, `headroom_after_suggestion`) | [../06-risk-guardrails/state-delivery.md §Guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) — same `per_rule[]` shape, parallel to the `validate_guardrail` tool |
| Greek and delta-adjusted exposure semantics | [../06-risk-guardrails/guardrail-evaluation.md](../06-risk-guardrails/guardrail-evaluation.md) |
| `feature_disabled` rejection reason | [../05-execution-layer/oms-command-schema.md](../05-execution-layer/oms-command-schema.md) |

---

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/submit-envelope-tool.json",
  "title": "submit_envelope tool",
  "description": "Input and response contract for the PM's submit_envelope tool. The tool accepts one PM envelope and returns one submission_result per embedded command.",
  "type": "object",
  "required": ["input", "response"],
  "properties": {
    "input": { "$ref": "#/$defs/input" },
    "response": { "$ref": "#/$defs/response" }
  },

  "$defs": {

    "input": {
      "type": "object",
      "required": ["envelope"],
      "properties": {
        "envelope": {
          "$ref": "https://alphamind.local/schemas/pm-envelope.json",
          "description": "One PM envelope. Validates against pm-envelope-schema.md. Embedded commands have command_id absent — the OMS command intake layer assigns IDs at receipt per oms-command-ids.md."
        }
      }
    },

    "response": {
      "type": "object",
      "required": ["envelope_id", "submission_results"],
      "properties": {
        "envelope_id": {
          "type": "string",
          "pattern": "^ENV-(REC|SA|SA-ORD)-[0-9]+$",
          "description": "Echoes the input envelope's envelope_id. Lets the PM correlate the response to the envelope it submitted when reasoning across multiple in-flight envelopes within an invocation."
        },
        "submission_results": {
          "type": "array",
          "description": "One entry per embedded command, in command_ordinal order. Length equals the input envelope's commands array length. Empty for envelopes carrying zero commands (rejection envelopes, hold envelopes).",
          "items": { "$ref": "#/$defs/submission_result" }
        }
      }
    },

    "submission_result": {
      "type": "object",
      "required": ["command_ordinal", "status"],
      "properties": {
        "command_ordinal": {
          "type": "integer",
          "minimum": 0,
          "description": "0-indexed position within the envelope's commands array. Matches the embedded command's structural position per oms-command-ids.md."
        },
        "status": {
          "type": "string",
          "enum": ["accepted", "rejected"]
        },
        "command_id": {
          "type": "string",
          "pattern": "^inv-[^.]+\\.ENV-(REC|SA|SA-ORD)-[0-9]+\\.[0-9]+\\.[0-9]+$",
          "description": "Engine-assigned command ID per oms-command-ids.md. Present on every result regardless of status — the rejected command was a first-class command for audit and feedback-loop purposes per oms-command-ids.md §Retry and modification model."
        },
        "acknowledgment": { "$ref": "#/$defs/acknowledgment" },
        "rejection_payload": { "$ref": "#/$defs/rejection_payload" }
      },
      "allOf": [
        {
          "if": { "properties": { "status": { "const": "accepted" } }, "required": ["status"] },
          "then": {
            "required": ["command_id", "acknowledgment"],
            "properties": { "rejection_payload": false }
          }
        },
        {
          "if": { "properties": { "status": { "const": "rejected" } }, "required": ["status"] },
          "then": {
            "required": ["command_id", "rejection_payload"],
            "properties": { "acknowledgment": false }
          }
        }
      ]
    },

    "acknowledgment": {
      "type": "object",
      "description": "Engine confirmation for an accepted command. Shape varies by command_type per the embedded command's semantics in oms-commands.md.",
      "properties": {
        "position_id": {
          "type": "string",
          "description": "Newly created position ID. Present on OPEN. Position enters 'pending' state until the entry order fills per oms-commands.md §OPEN."
        },
        "order_id": {
          "type": "string",
          "description": "Broker order ID assigned at submission. Present on OPEN, CLOSE (when not 'all' against a fully-filled bracket), ADJUST (replacement order), ADD."
        },
        "validation_metadata": {
          "type": "object",
          "description": "Validation-time computation results. Present on OPEN and ADD for options and strategies per oms-commands.md §OPEN — Greek computation at validation time. Carries the greeks computed by the guardrail layer, the IV used, the resulting delta-adjusted exposure, and per-rule headroom against each applicable limit.",
          "properties": {
            "greeks": { "$ref": "#/$defs/greeks" },
            "implied_volatility": { "type": "number", "exclusiveMinimum": 0 },
            "delta_adjusted_exposure": { "type": "number" },
            "per_rule_headroom": {
              "type": "array",
              "items": { "$ref": "#/$defs/per_rule_headroom_entry" }
            }
          }
        },
        "released_capital_usd": {
          "type": "number",
          "minimum": 0,
          "description": "Capital returned to available buying power. Present on CANCEL per oms-commands.md §CANCEL — Capital release."
        }
      }
    },

    "rejection_payload": {
      "type": "object",
      "required": ["rules_breached", "suggested_modification"],
      "description": "Synchronous rejection record per breach-behavior.md §Hard rejection semantics. The PM uses this to revise the command and re-invoke submit_envelope.",
      "properties": {
        "rules_breached": {
          "type": "array",
          "minItems": 1,
          "description": "One entry per guardrail that blocked the command. Per-rule field shape parallels the validate_guardrail tool's per_rule[] shape per state-delivery.md §Guardrail validation tool.",
          "items": { "$ref": "#/$defs/breached_rule" }
        },
        "suggested_modification": {
          "type": "string",
          "description": "Mechanical compliance suggestion (e.g., 'reduce size by 42%', 'switch to a strike with delta below 0.30'). Starting point for the PM, not binding."
        },
        "headroom_after_suggestion": {
          "type": "array",
          "description": "Projected per-rule headroom if the PM adopts the suggested modification verbatim. One entry per breached rule, keyed by rule name.",
          "items": { "$ref": "#/$defs/per_rule_headroom_entry" }
        },
        "greeks": {
          "$ref": "#/$defs/greeks",
          "description": "Computed greeks for the rejected command. Present for options and strategy commands per oms-commands.md §OPEN — On rejection."
        },
        "delta_adjusted_exposure": {
          "type": "number",
          "description": "Delta-adjusted exposure that triggered the rejection. Present for options and strategy commands."
        },
        "feature_disabled": {
          "type": "string",
          "enum": ["options", "short_selling", "sector"],
          "description": "Present when the rejection reason is feature-flag denial per the active portfolio profile (see oms-command-schema.md §Feature-flag interaction). Mutually exclusive with rule-based breaches — when set, rules_breached carries a single synthetic entry with rule = 'feature_disabled'."
        }
      }
    },

    "breached_rule": {
      "type": "object",
      "required": ["rule", "current", "limit", "overage", "unit"],
      "properties": {
        "rule": {
          "type": "string",
          "description": "Canonical rule name from rules-and-limits.md (e.g., 'sector_concentration', 'gross_exposure_pct', 'options_delta_pct')."
        },
        "current": {
          "type": "number",
          "description": "Current value for this rule prior to the rejected command (e.g., 23.7 for 'sector tech delta-adjusted exposure: 23.7%')."
        },
        "limit": {
          "type": "number",
          "description": "Active limit for this rule under the current regime and profile (e.g., 25.0 for 'sector tech limit: 25.0%')."
        },
        "overage": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "How much the command would exceed the limit (e.g., 3.2 for 'would push to 28.2%, overage: 3.2%')."
        },
        "unit": {
          "type": "string",
          "description": "Unit string per state-delivery.md §Guardrail validation tool (e.g., '% of portfolio (delta-adjusted)', 'USD', 'contracts')."
        }
      }
    },

    "per_rule_headroom_entry": {
      "type": "object",
      "required": ["rule", "headroom_remaining", "unit"],
      "properties": {
        "rule": { "type": "string" },
        "headroom_remaining": { "type": "number" },
        "unit": { "type": "string" }
      }
    },

    "greeks": {
      "type": "object",
      "required": ["delta", "gamma", "theta", "vega"],
      "properties": {
        "delta": { "type": "number" },
        "gamma": { "type": "number" },
        "theta": { "type": "number" },
        "vega": { "type": "number" }
      }
    }
  }
}
```

---

## Notes on cross-field invariants

Enforced by the engine's submission tool implementation rather than the schema:

- **`submission_results[]` length equals the input envelope's `commands[]` length.** The engine produces one result per embedded command, in `command_ordinal` order. Empty `commands[]` on the input envelope yields empty `submission_results[]` on the response — the tool is still callable on rejection envelopes and hold envelopes for symmetry.

- **`command_ordinal` values are 0-indexed and contiguous.** Mirror the input envelope's array indices. No gaps, no duplicates.

- **`command_id` on accepted results matches the deterministic ID derivation in [oms-command-ids.md](../oms-command-ids.md).** The PM does not parse or reason about the ID — it is opaque from the agent's perspective and exists for audit-log correlation.

- **Rejection does not abort envelope processing.** Per [portfolio-manager.md §Synchronous command feedback](portfolio-manager.md#synchronous-command-feedback): commands process sequentially with cumulative state accounting; a rejected command is excluded from cumulative state for subsequent commands but does not prevent them from being attempted. Each command in `commands[]` produces a `submission_result` regardless of prior commands' outcomes within the same envelope.

- **Re-submission after rejection is a fresh envelope receipt.** When the PM appends a `post_rejection` modification and re-invokes `submit_envelope`, the new call's `attempt_seq` increments per [oms-command-ids.md §attempt_seq computation](../oms-command-ids.md#attempt_seq-computation), producing distinct `command_id` values. The tool itself is stateless — sequencing and dedup live in the OMS command intake layer.

- **`feature_disabled` rejections short-circuit per-rule evaluation.** When set, `rules_breached` carries one synthetic entry with `rule: 'feature_disabled'`; the rest of the per-rule shape is omitted because no rule-based evaluation occurred. Behavior parallels [guardrail-evaluation.md](../06-risk-guardrails/guardrail-evaluation.md): disabled features have no rule rows in the output.

- **`acknowledgment` and `rejection_payload` are mutually exclusive on a single result.** Schema-enforced via the `status`-conditional `false` clauses on the alternate field. The status discriminates which payload is populated.

---

## Evolution

The tool's input/output contract lands here first; prose updates in [portfolio-manager.md](portfolio-manager.md) follow. The tool is the single transport for PM→engine command submission — additions to the acknowledgment shape (e.g., new validation metadata for a future instrument class) or rejection payload shape (e.g., a new feature-flag class) require a coordinated update here, in [breach-behavior.md](../06-risk-guardrails/breach-behavior.md), and in the [validate_guardrail tool contract](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) when the field is shared between the two surfaces.

The discriminated `status` shape is load-bearing — the PM's response handling branches on `accepted` vs `rejected`. Adding a third status (e.g., `deferred`) would require coordinated updates to [portfolio-manager.md §Synchronous command feedback](portfolio-manager.md#synchronous-command-feedback) and to the PM's reasoning loop documented there.

---

## Cross-references

- PM envelope schema (input shape): [pm-envelope-schema.md](pm-envelope-schema.md)
- OMS command schema (embedded command shape): [../05-execution-layer/oms-command-schema.md](../05-execution-layer/oms-command-schema.md)
- OMS command ID derivation: [../oms-command-ids.md](../oms-command-ids.md)
- PM synchronous feedback prose: [portfolio-manager.md §Synchronous command feedback](portfolio-manager.md#synchronous-command-feedback)
- Hard rejection semantics: [../06-risk-guardrails/breach-behavior.md §Hard rejection semantics](../06-risk-guardrails/breach-behavior.md#hard-rejection-semantics)
- Per-rule shape conventions and sibling pre-submission validation tool (`validate_guardrail`): [../06-risk-guardrails/state-delivery.md §Guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool)
- Sequencing within an invocation: [../05-execution-layer/oms-commands.md §Sequencing within an invocation](../05-execution-layer/oms-commands.md#sequencing-within-an-invocation)
- Tool registration in agent configuration: [../configuration-management.md §agents.yaml](../configuration-management.md#agentsyaml)
- LLM tool-use integration: [../architecture/llm-integration.md](../architecture/llm-integration.md)
