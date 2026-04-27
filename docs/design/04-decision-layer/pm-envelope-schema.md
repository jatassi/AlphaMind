# PM envelope schema

Formal JSON Schema (Draft 2020-12) for the command envelopes the portfolio manager produces. Machine-readable contract for [portfolio-manager.md — Envelope structure](portfolio-manager.md#envelope-structure). Every envelope must validate against this schema at the LLM output validation seam before commands reach the OMS.

Engine-originated envelopes: [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md).

## Scope

- **Contract surface:** one PM envelope at a time. Envelopes carry zero or more OMS commands plus the PM's evaluation context. The PM emits one envelope per source proposal (analyst recommendation, strategist position assessment, strategist pending-order assessment). A single invocation typically produces several.
- **LLM vs. infrastructure populated:** the PM produces every field except `command_id` on embedded commands (assigned by the OMS command intake layer per [oms-command-ids.md](../oms-command-ids.md)). The LLM output validator checks envelopes with `command_id` omitted.
- **Source provenance variants:** `pm_analyst` (evaluating an analyst new-entry proposal) and `pm_strategist` (evaluating a strategist position or pending-order assessment). Each variant has distinct required fields.
- **Rejection envelopes are first-class.** Envelopes with `verdict: reject` carry an empty `commands` array and populated `concerns` and `rationale_narrative`. The feedback loop reads rejections as structured records.
- **Feature-flag interaction:** the schema permits commands for any instrument class. The guardrail validation layer rejects commands whose instrument class is disabled by the active [portfolio profile](../06-risk-guardrails/rules-and-limits.md#dual-portfolio-profiles).

## Cross-references

Enum values and structural constraints trace back to these sources:

| Field | Source |
|---|---|
| `source_provenance` enum (PM subset) | [portfolio-manager.md — Envelope structure](portfolio-manager.md#envelope-structure) |
| `envelope_id` format | [oms-command-ids.md](../oms-command-ids.md) |
| `verdict` enum | [portfolio-manager.md — Verdict aggregation](portfolio-manager.md#verdict-aggregation) |
| `recommendation_type` enum | [portfolio-manager.md — Envelope structure](portfolio-manager.md#envelope-structure) |
| `evaluation` criteria (pm_analyst: 5 keys) | [portfolio-manager.md — Thesis quality evaluation](portfolio-manager.md#thesis-quality-evaluation-new-entry-proposals) |
| `evaluation` criteria (pm_strategist: 4 keys) | [portfolio-manager.md — Position-action evaluation](portfolio-manager.md#position-action-evaluation-strategist-recommendations) |
| `modifications[].phase` | [oms-command-ids.md — attempt_seq computation](../oms-command-ids.md#attempt_seq-computation) |
| `modifications[].adjustment_category` | [portfolio-manager.md — PM-originated envelopes](portfolio-manager.md#pm-originated-envelopes-pm_analyst-and-pm_strategist) |
| Commands array content | [../05-execution-layer/oms-command-schema.md](../05-execution-layer/oms-command-schema.md) |
| Anti-pattern names | [portfolio-manager.md — Anti-patterns](portfolio-manager.md#anti-patterns-the-pm-is-watching-for) |

---

## Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/pm-envelope.json",
  "title": "PM command envelope",
  "description": "One envelope produced by the portfolio manager agent. Discriminated union on source_provenance.",
  "type": "object",
  "required": [
    "envelope_id",
    "invocation_id",
    "source_provenance",
    "source_recommendation_id",
    "recommendation_type",
    "verdict",
    "evaluation",
    "modifications",
    "concerns",
    "rationale_narrative",
    "commands"
  ],
  "properties": {
    "envelope_id": {
      "type": "string",
      "pattern": "^ENV-(REC|SA|SA-ORD)-[0-9]+$",
      "description": "Bijective with the source proposal. ENV-REC-<n> for analyst proposals, ENV-SA-<n> for strategist position assessments, ENV-SA-ORD-<n> for strategist pending-order assessments. See oms-command-ids.md for the derivation rules."
    },
    "invocation_id": {
      "type": "string",
      "description": "Pipeline invocation this envelope belongs to. Always present for PM-originated envelopes."
    },
    "source_provenance": {
      "type": "string",
      "enum": ["pm_analyst", "pm_strategist"],
      "description": "Identifies the envelope's origin within the PM's surface."
    },
    "source_recommendation_id": {
      "type": "string",
      "description": "The proposal this envelope evaluates. REC-<n> for analyst, SA-<n> for strategist position assessments, SA-ORD-<n> for strategist pending-order assessments. Must match an ID in the invocation's source output."
    },
    "recommendation_type": {
      "type": "string",
      "enum": ["new_entry", "position_assessment", "pending_order_assessment"]
    },
    "position_id": {
      "type": "string",
      "description": "The position this envelope's assessment addresses. Required for pm_strategist envelopes; absent for pm_analyst envelopes."
    },
    "verdict": {
      "type": "string",
      "enum": ["approve", "approve_with_modification", "reject"]
    },
    "evaluation": {
      "description": "Per-criterion pass/fail assessment. Shape depends on source_provenance — five keys for pm_analyst, four keys for pm_strategist.",
      "oneOf": [
        { "$ref": "#/$defs/thesis_quality_evaluation" },
        { "$ref": "#/$defs/position_action_evaluation" }
      ]
    },
    "modifications": {
      "type": "array",
      "description": "Zero or more structured records of what the PM changed. Empty for pass-through approvals and rejections.",
      "items": { "$ref": "#/$defs/modification_record" }
    },
    "concerns": {
      "type": "array",
      "description": "Structured concerns list. Populated from failed criteria (one entry per failure, naming the criterion) plus any additional PM concerns not captured by a criterion.",
      "items": { "$ref": "#/$defs/concern_record" }
    },
    "rationale_narrative": {
      "type": "string",
      "description": "Prose explanation naming the failure pattern where applicable, explaining cross-criterion interactions, documenting the reasoning behind the verdict."
    },
    "anti_patterns_identified": {
      "type": "array",
      "description": "Canonical anti-pattern strings the feedback loop aggregates on. See portfolio-manager.md — Anti-patterns.",
      "items": {
        "type": "string",
        "enum": [
          "conviction_inflation",
          "sunk_cost_persistence",
          "rationalized_continuation",
          "thesis_contradiction_suppression",
          "engine_originated_closure_signal"
        ]
      }
    },
    "commands": {
      "type": "array",
      "description": "Zero or more OMS commands. Empty for rejection envelopes and for hold envelopes on strategist assessments where no command is needed. Each item validates against oms-command-schema.json with the additional constraint that CLOSE commands with close_rationale_type = risk_management must use risk_management_subtype = pm_directed.",
      "items": {
        "allOf": [
          { "$ref": "https://alphamind.local/schemas/oms-command.json" },
          {
            "if": {
              "properties": {
                "command_type": { "const": "close" },
                "close_rationale_type": { "const": "risk_management" }
              },
              "required": ["command_type", "close_rationale_type"]
            },
            "then": {
              "properties": {
                "risk_management_subtype": { "const": "pm_directed" }
              },
              "required": ["risk_management_subtype"]
            }
          }
        ]
      }
    }
  },
  "oneOf": [
    { "$ref": "#/$defs/pm_analyst_envelope" },
    { "$ref": "#/$defs/pm_strategist_envelope" }
  ],

  "$defs": {

    "pm_analyst_envelope": {
      "type": "object",
      "properties": {
        "envelope_id": { "pattern": "^ENV-REC-[0-9]+$" },
        "source_provenance": { "const": "pm_analyst" },
        "source_recommendation_id": {
          "pattern": "^REC-[0-9]+$",
          "description": "Must match a recommendation_id in the invocation's analyst output per analyst-output-schema.md."
        },
        "recommendation_type": { "const": "new_entry" },
        "evaluation": { "$ref": "#/$defs/thesis_quality_evaluation" },
        "position_id": false
      },
      "required": ["envelope_id", "source_provenance", "source_recommendation_id", "recommendation_type", "evaluation"],
      "allOf": [
        {
          "if": { "properties": { "verdict": { "const": "reject" } }, "required": ["verdict"] },
          "then": {
            "properties": {
              "commands": { "maxItems": 0 },
              "modifications": { "maxItems": 0 },
              "concerns": { "minItems": 1 }
            }
          }
        },
        {
          "if": { "properties": { "verdict": { "const": "approve" } }, "required": ["verdict"] },
          "then": {
            "properties": {
              "modifications": { "maxItems": 0 }
            }
          }
        },
        {
          "if": { "properties": { "verdict": { "const": "approve_with_modification" } }, "required": ["verdict"] },
          "then": {
            "properties": {
              "modifications": { "minItems": 1 },
              "commands": { "minItems": 1 }
            }
          }
        }
      ]
    },

    "pm_strategist_envelope": {
      "type": "object",
      "properties": {
        "envelope_id": { "pattern": "^ENV-(SA|SA-ORD)-[0-9]+$" },
        "source_provenance": { "const": "pm_strategist" },
        "source_recommendation_id": {
          "pattern": "^SA(-ORD)?-[0-9]+$",
          "description": "Must match an assessment_id in the invocation's strategist output per strategist-output-schema.md."
        },
        "recommendation_type": { "enum": ["position_assessment", "pending_order_assessment"] },
        "evaluation": { "$ref": "#/$defs/position_action_evaluation" }
      },
      "required": ["envelope_id", "source_provenance", "source_recommendation_id", "recommendation_type", "position_id", "evaluation"],
      "allOf": [
        {
          "if": { "properties": { "recommendation_type": { "const": "position_assessment" } }, "required": ["recommendation_type"] },
          "then": { "properties": { "envelope_id": { "pattern": "^ENV-SA-[0-9]+$" } } }
        },
        {
          "if": { "properties": { "recommendation_type": { "const": "pending_order_assessment" } }, "required": ["recommendation_type"] },
          "then": { "properties": { "envelope_id": { "pattern": "^ENV-SA-ORD-[0-9]+$" } } }
        },
        {
          "if": { "properties": { "verdict": { "const": "reject" } }, "required": ["verdict"] },
          "then": {
            "properties": {
              "commands": { "maxItems": 0 },
              "modifications": { "maxItems": 0 },
              "concerns": { "minItems": 1 }
            }
          }
        },
        {
          "if": { "properties": { "verdict": { "const": "approve" } }, "required": ["verdict"] },
          "then": {
            "properties": {
              "modifications": { "maxItems": 0 }
            }
          }
        },
        {
          "if": { "properties": { "verdict": { "const": "approve_with_modification" } }, "required": ["verdict"] },
          "then": {
            "properties": {
              "modifications": { "minItems": 1 },
              "commands": { "minItems": 1 }
            }
          }
        }
      ]
    },

    "thesis_quality_evaluation": {
      "type": "object",
      "description": "Per-criterion pass/fail assessment for analyst new-entry proposals per portfolio-manager.md — Thesis quality evaluation.",
      "required": [
        "falsifiability",
        "sizing_proportionality",
        "portfolio_coherence",
        "timing_plausibility",
        "counterargument_consideration"
      ],
      "properties": {
        "falsifiability": { "$ref": "#/$defs/criterion_assessment" },
        "sizing_proportionality": { "$ref": "#/$defs/criterion_assessment" },
        "portfolio_coherence": { "$ref": "#/$defs/criterion_assessment" },
        "timing_plausibility": { "$ref": "#/$defs/criterion_assessment" },
        "counterargument_consideration": { "$ref": "#/$defs/criterion_assessment" }
      }
    },

    "position_action_evaluation": {
      "type": "object",
      "description": "Per-criterion pass/fail assessment for strategist position-action recommendations per portfolio-manager.md — Position-action evaluation.",
      "required": [
        "status_classification_warrant",
        "action_status_alignment",
        "action_specific_justification",
        "portfolio_coherence"
      ],
      "properties": {
        "status_classification_warrant": { "$ref": "#/$defs/criterion_assessment" },
        "action_status_alignment": { "$ref": "#/$defs/criterion_assessment" },
        "action_specific_justification": { "$ref": "#/$defs/criterion_assessment" },
        "portfolio_coherence": { "$ref": "#/$defs/criterion_assessment" }
      }
    },

    "criterion_assessment": {
      "type": "object",
      "required": ["status"],
      "properties": {
        "status": { "enum": ["pass", "fail"] },
        "note": {
          "type": "string",
          "description": "Optional short note. Full reasoning lives in the envelope's rationale_narrative; per-criterion notes are for the structured surface the feedback loop aggregates on."
        }
      }
    },

    "modification_record": {
      "type": "object",
      "required": [
        "phase",
        "field_changed",
        "original_value",
        "approved_value",
        "adjustment_category",
        "rationale"
      ],
      "properties": {
        "phase": {
          "type": "string",
          "enum": ["pre_submission", "post_rejection"],
          "description": "Distinguishes PM-authored modifications (pre_submission) from modifications appended after a synchronous guardrail rejection (post_rejection). The OMS command intake layer counts post_rejection entries to derive command_id.attempt_seq per oms-command-ids.md."
        },
        "field_changed": {
          "type": "string",
          "description": "The command field whose value was modified (e.g., 'position_size.quantity', 'invalidation_legs[0].condition.trigger_price')."
        },
        "original_value": {
          "description": "The value as proposed by the analyst or strategist. Type matches the field's type."
        },
        "approved_value": {
          "description": "The value the PM approved. Type matches the field's type."
        },
        "adjustment_category": {
          "type": "string",
          "enum": [
            "risk_reduction",
            "conviction_disagreement",
            "capital_constraint",
            "portfolio_balance",
            "guardrail_rejection_response"
          ],
          "description": "Classification of why the PM modified the proposal."
        },
        "rationale": {
          "type": "string",
          "description": "Per-modification explanation. Distinct from the envelope's overall rationale_narrative."
        },
        "triggering_rule": {
          "type": "string",
          "description": "Required when adjustment_category is 'guardrail_rejection_response'. The specific guardrail rule whose rejection drove this modification (e.g., 'sector_concentration')."
        }
      },
      "allOf": [
        {
          "if": { "properties": { "adjustment_category": { "const": "guardrail_rejection_response" } }, "required": ["adjustment_category"] },
          "then": {
            "required": ["triggering_rule"],
            "properties": { "phase": { "const": "post_rejection" } }
          }
        },
        {
          "if": { "properties": { "adjustment_category": { "not": { "const": "guardrail_rejection_response" } } }, "required": ["adjustment_category"] },
          "then": {
            "properties": { "phase": { "const": "pre_submission" } }
          }
        }
      ]
    },

    "concern_record": {
      "type": "object",
      "required": ["source", "summary"],
      "properties": {
        "source": {
          "type": "string",
          "description": "What produced this concern — either a failed criterion name (matching an evaluation key) or 'other' for PM concerns not captured by a criterion."
        },
        "summary": {
          "type": "string",
          "description": "Short description of the concern. The rationale_narrative carries the full reasoning."
        }
      }
    }
  }
}
```

---

## Notes on cross-field invariants

Enforced by the OMS command intake layer rather than the schema:

- **`envelope_id` source-ID correspondence.** The integer portion of an `ENV-REC-<n>` / `ENV-SA-<n>` / `ENV-SA-ORD-<n>` envelope ID matches the integer portion of its `source_recommendation_id`. The bijection lets the intake layer derive the envelope ID from the source ID without additional metadata.

- **`source_recommendation_id` must exist in the current invocation's source output.** For `pm_analyst`, `REC-<n>` matches a recommendation_id in the analyst output. For `pm_strategist`, `SA-<n>` or `SA-ORD-<n>` matches an assessment_id in the strategist output.

- **`position_id` must reference an open position** for `pm_strategist` envelopes and for commands referencing positions.

- **`recommendation_type: pending_order_assessment` envelopes carry `cancel` or `adjust` commands, not `close` or `add`.** A pending-order assessment acts on an order, not a position; the restriction follows from the strategist's pending-order action enum (`maintain` / `modify` / `cancel` per [strategist-output-schema.md](strategist-output-schema.md), translating to omit-envelope / `adjust` command / `cancel` command).

- **`attempt_seq` on embedded command IDs corresponds to the envelope's `post_rejection` modification count.** Assigned by the intake layer at receipt per [oms-command-ids.md §attempt_seq computation](../oms-command-ids.md#attempt_seq-computation). A mismatch aborts the invocation.

- **PM-originated CLOSE commands with `close_rationale_type: risk_management` use `risk_management_subtype: pm_directed`.** Schema-enforced via a conditional on embedded commands.

- **Anti-pattern names match the canonical strings used across the system.** The `anti_patterns_identified` enum is the single authoritative list aggregated on by the feedback loop. Drift between strategist self-identification, PM detection, and this schema would silently fragment the aggregation surface.

- **Rejection envelopes populate at least one `concerns` entry.** Schema-enforced via `concerns: { minItems: 1 }` in the `verdict == reject` conditional of both per-provenance branches. The feedback loop's "what proposals does the PM reject and why" depends on every rejection carrying its failure structure; an `"other"`-source `concern_record` accommodates rejections not captured by a named criterion.

---

## Evolution

When envelope semantics change — a new adjustment category, anti-pattern name, or verdict option — they are added here first; this schema is the authoritative contract. Prose updates in [portfolio-manager.md](portfolio-manager.md) follow.

Adding a new `adjustment_category` requires updating both the enum and the conditional phase-association rule. The current rule (`guardrail_rejection_response` is post-submission; everything else is pre-submission) is structural — the phase partition lets the intake layer derive `attempt_seq` without side state.

Adding a new anti-pattern requires coordinated updates to:
- [portfolio-manager.md — Anti-patterns](portfolio-manager.md#anti-patterns-the-pm-is-watching-for) (prose)
- [strategist.md — LLM failure mode avoidance](strategist.md#llm-failure-mode-avoidance) (self-identification list if applicable)
- This schema's `anti_patterns_identified` enum
- [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md) if engine-observable
- Any Phase 4 feedback-loop aggregation consuming the string

---

## Cross-references

- PM envelope prose contract: [portfolio-manager.md](portfolio-manager.md)
- Engine-originated envelope schema: [../05-execution-layer/engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md)
- OMS command schema: [../05-execution-layer/oms-command-schema.md](../05-execution-layer/oms-command-schema.md)
- Command ID discipline and envelope bijection: [../oms-command-ids.md](../oms-command-ids.md)
- Analyst output schema (REC-N IDs): [analyst-output-schema.md](analyst-output-schema.md)
- Strategist output schema (SA-N and SA-ORD-N IDs): [strategist-output-schema.md](strategist-output-schema.md)
- LLM output validation mechanism: [../testing/llm-output-validation.md](../testing/llm-output-validation.md)
