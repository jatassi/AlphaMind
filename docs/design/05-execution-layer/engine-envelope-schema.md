# Engine envelope schema

Formal JSON Schema (Draft 2020-12) for engine-originated command envelopes produced by the continuous monitor between invocations. Machine-readable counterpart to the prose in [portfolio-manager.md — Engine-originated envelopes](../04-decision-layer/portfolio-manager.md#engine-originated-envelopes-engine_guardrail). One envelope per breach trigger; each carries exactly one protective CLOSE command. The OMS command intake layer validates every engine-originated envelope against this schema on receipt.

PM-originated envelopes are specified in [pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md).

## Scope

- **Contract surface:** one envelope, one OMS command, plus the motivating guardrail trigger record. Cascades (margin call + forced reduction, primary + secondary breach) produce multiple envelopes linked by a shared `cascade_id`, not multiple commands per envelope.
- **Producer:** the continuous monitor process ([architecture.md §4](architecture.md)). Deterministic code that evaluates live quote data against portfolio state, detects breaches per [breach-behavior.md](../06-risk-guardrails/breach-behavior.md), and issues protective CLOSE commands.
- **Command shape:** every command is a CLOSE with `close_rationale_type: risk_management` and `risk_management_subtype: engine_guardrail`.
- **Timing model:** envelopes are generated between pipeline invocations and inside an invocation's collect phase. They carry `trigger_timestamp` as the time anchor rather than `invocation_id`.
- **Feature-flag interaction:** none direct. The monitor can only close existing positions; closing is never rejected on instrument-class grounds even when the portfolio profile disables that instrument class for new entries.

## Cross-references

Enum values and structural constraints trace back to these authoritative sources:

| Field | Source |
|---|---|
| `source_provenance` | [portfolio-manager.md — Engine-originated envelopes](../04-decision-layer/portfolio-manager.md#engine-originated-envelopes-engine_guardrail) |
| `envelope_id` format | [../oms-command-ids.md — Engine-originated command IDs](../oms-command-ids.md#engine-originated-command-ids) |
| `guardrail_trigger_record.rule_breached` | [../06-risk-guardrails/rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md) |
| `cascade_id` semantics | [../06-risk-guardrails/breach-behavior.md](../06-risk-guardrails/breach-behavior.md) |
| `position_selection_rationale` discipline | [../06-risk-guardrails/breach-behavior.md](../06-risk-guardrails/breach-behavior.md) |
| Secondary-breach check result | [oms-commands.md — Command origins](oms-commands.md#command-origins) |
| Commands array content | [oms-command-schema.md](oms-command-schema.md) |

---

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/engine-envelope.json",
  "title": "Engine-originated command envelope",
  "description": "One envelope produced by the continuous monitor in response to a guardrail breach.",
  "type": "object",
  "required": [
    "envelope_id",
    "trigger_timestamp",
    "source_provenance",
    "guardrail_trigger_record",
    "commands"
  ],
  "properties": {
    "envelope_id": {
      "type": "string",
      "pattern": "^MON\\.[^.]+\\.[0-9]+$",
      "description": "MON.{monitor_session_id}.{trigger_id}. See oms-command-ids.md — Engine-originated command IDs."
    },
    "invocation_id": {
      "type": "null",
      "description": "Always null for engine-originated envelopes. trigger_timestamp is the time anchor."
    },
    "trigger_timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "When the breach was detected by the continuous monitor."
    },
    "source_provenance": {
      "type": "string",
      "const": "engine_guardrail"
    },
    "guardrail_trigger_record": { "$ref": "#/$defs/guardrail_trigger_record" },
    "commands": {
      "type": "array",
      "minItems": 1,
      "maxItems": 1,
      "description": "Exactly one CLOSE command per trigger. Cascades produce multiple envelopes linked by cascade_id, not multiple commands per envelope.",
      "items": {
        "allOf": [
          { "$ref": "https://alphamind.local/schemas/oms-command.json" },
          {
            "properties": {
              "command_type": { "const": "close" },
              "close_rationale_type": { "const": "risk_management" },
              "risk_management_subtype": { "const": "engine_guardrail" }
            },
            "required": ["command_type", "close_rationale_type", "risk_management_subtype"]
          }
        ]
      }
    }
  },

  "$defs": {

    "guardrail_trigger_record": {
      "type": "object",
      "description": "The breach context that motivated this protective CLOSE. Populated by the continuous monitor at trigger time.",
      "required": [
        "rule_breached",
        "trigger_timestamp",
        "breach_details",
        "position_selection_rationale"
      ],
      "properties": {
        "rule_breached": {
          "type": "string",
          "description": "The guardrail rule identifier that triggered the protective CLOSE (e.g., 'per_position_max_loss', 'sector_concentration', 'daily_drawdown', 'margin_call'). See rules-and-limits.md for the authoritative rule set."
        },
        "trigger_timestamp": {
          "type": "string",
          "format": "date-time",
          "description": "When the breach was detected. Matches the envelope's top-level trigger_timestamp."
        },
        "breach_details": {
          "type": "object",
          "required": ["current_value", "limit_value", "overage"],
          "properties": {
            "current_value": { "type": "number" },
            "limit_value": { "type": "number" },
            "overage": {
              "type": "number",
              "description": "Signed magnitude of the breach. Positive for overages; negative for deficit direction cases where the sign is clearer that way."
            },
            "unit": { "type": "string" },
            "regime_at_breach": {
              "type": "string",
              "description": "Active regime at the time of breach — the parameter set the limit was drawn from."
            }
          }
        },
        "position_selection_rationale": {
          "type": "string",
          "description": "Why this specific position was selected for reduction — e.g., 'smallest position in breaching sector', 'position with highest unrealized loss', 'position triggering the position-level max loss limit'. Deterministic per breach-behavior.md."
        },
        "cascade_id": {
          "type": "string",
          "description": "Present when this trigger is part of a cascade (margin call + forced reduction, primary breach + secondary breach). Shared across all activity log entries in the cascade. See breach-behavior.md."
        },
        "secondary_breach_check_result": {
          "type": "object",
          "description": "Present when the position selection required a secondary-breach check per oms-commands.md — Command origins. Populated by the continuous monitor during selection.",
          "required": ["result"],
          "properties": {
            "result": { "enum": ["no_secondary_breach", "secondary_breach_avoided", "deferred_to_pm"] },
            "notes": { "type": "string" }
          }
        }
      }
    }
  }
}
```

---

## Notes on cross-field invariants

Invariants enforced by the OMS command intake layer or the continuous monitor (not expressible in JSON Schema):

- **`envelope_id.monitor_session_id` must match the monitor's current session ID.** A monitor restart creates a new session; envelopes from a prior session are rejected as stale.

- **`envelope_id.trigger_id` is monotonically increasing within a session.** Trigger IDs are assigned in receipt order; out-of-order or gapped IDs are a structural error.

- **The embedded command's `command_id` uses the `MON.{session}.{trigger}.{ordinal}` format**, enforced by the [oms-command-schema.md](oms-command-schema.md) command ID pattern with the envelope's source_provenance as the discriminator.

- **Position selection is deterministic.** `position_selection_rationale` names the deterministic rule used. A rationale string not matching a documented selection rule in [breach-behavior.md](../06-risk-guardrails/breach-behavior.md) is a structural error.

- **Cascades produce multiple envelopes, not multiple commands.** Each cascade stage (margin call → forced reduction → secondary breach avoidance → final close) emits its own envelope with its own `trigger_id` and the shared `cascade_id`. The `maxItems: 1` on `commands` enforces this structurally.

- **`trigger_timestamp` at the top level and inside `guardrail_trigger_record` must match.** Denormalized for OMS command intake layer convenience; equality is a structural invariant.

---

## Evolution

Schema changes land here first; prose in [portfolio-manager.md — Engine-originated envelopes](../04-decision-layer/portfolio-manager.md#engine-originated-envelopes-engine_guardrail) and [breach-behavior.md](../06-risk-guardrails/breach-behavior.md) follows.

The single-CLOSE-per-envelope restriction is load-bearing for the cascade model. If cascades ever need to atomically close multiple positions in a single trigger, `maxItems: 1` relaxes here and cascade semantics in [breach-behavior.md](../06-risk-guardrails/breach-behavior.md) update in coordination.

---

## Cross-references

- PM envelope schema (sibling contract for LLM-produced envelopes): [../04-decision-layer/pm-envelope-schema.md](../04-decision-layer/pm-envelope-schema.md)
- OMS command schema (what the embedded CLOSE command conforms to): [oms-command-schema.md](oms-command-schema.md)
- Command ID discipline (engine-originated format): [../oms-command-ids.md](../oms-command-ids.md)
- Continuous monitor responsibilities: [architecture.md §4](architecture.md)
- Breach behavior and cascade semantics: [../06-risk-guardrails/breach-behavior.md](../06-risk-guardrails/breach-behavior.md)
- Rules and limits (source of rule_breached values): [../06-risk-guardrails/rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)
- Command origins (prose contract for engine-originated commands): [oms-commands.md — Command origins](oms-commands.md#command-origins)
