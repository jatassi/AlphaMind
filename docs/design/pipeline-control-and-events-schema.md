# Pipeline control and events schema

Formal JSON Schema (Draft 2020-12) for the pipeline process's loopback HTTP surface — request bodies and response envelopes for `POST /control/*`, plus the per-event payloads emitted on the `GET /events` SSE stream. Machine-readable counterpart to [command-center.md § Control surface](command-center.md#control-surface) and [§ Live event stream](command-center.md#live-event-stream). The command-center backend validates every request body before forwarding and every event payload before re-emitting downstream.

Monitor wire format is specified separately in [monitor-control-and-events-schema.md](monitor-control-and-events-schema.md) — pipeline and continuous monitor are distinct producers of distinct event taxonomies.

## Scope

- **Producer:** the pipeline process. FastAPI + Uvicorn bound to `127.0.0.1` within the same OS process that hosts APScheduler and the per-invocation orchestrator. The command-center backend is the only client; loopback isolation is the trust boundary.
- **Surface:** five `POST /control/*` verbs (pause, resume, trigger emergency invocation, switch profile, run universe validation) plus one `GET /events` SSE stream carrying nine event types (eight operational, one heartbeat).
- **What is and is not in scope:** request bodies, success response envelopes, error response envelope, SSE framing, and per-event payload shapes are in scope. Authentication is not — the surface is loopback-bound and the public-facing `/api/control/*` proxy on the command-center backend owns auth and audit per [command-center.md § Control surface](command-center.md#control-surface). HTTP status code semantics follow standard HTTP and FastAPI conventions and are noted in the per-verb tables; the schemas describe body shapes only.
- **Header conventions:** `Content-Type: application/json` on every control request and response with a JSON body. `Content-Type: text/event-stream` with `Cache-Control: no-cache` and `Connection: keep-alive` on the `GET /events` response. No request-id or correlation header — the command-center backend is the only client and owns its own correlation in the audit trail.
- **SSE framing.** Each event is emitted as one SSE record terminated by a blank line:

  ```
  event: <event_name>
  data: <json>

  ```

  The `event` field carries the event name from the table in [Pipeline events](#pipeline-events); the `data` field carries the event payload as a single-line JSON document conforming to the matching `$defs/<name>_event` definition. The `id` SSE field is not emitted — pipeline events have no historical guarantee per [command-center.md § Live event stream](command-center.md#live-event-stream); browsers reconnect fresh and there is no `Last-Event-ID` resume.

- **Liveness:** the `heartbeat` event fires every 15 seconds when no other event has been emitted, so a healthy connection always has a record within the last 15 seconds. The command-center backend's reconnect logic uses idle-timeout > 30 seconds as its disconnect signal.

## Cross-references

Enum values and structural constraints trace back to these authoritative sources:

| Field | Source |
|---|---|
| `POST /control/*` verb set | [command-center.md § Control surface — Pipeline endpoints](command-center.md#control-surface) |
| Event name set | [command-center.md § Live event stream — Pipeline events](command-center.md#live-event-stream) |
| `run_type` enum | [infrastructure.md § Trigger schedule](../architecture/infrastructure.md) |
| `phase` enum | [pipeline-overview.md § Layer overview](pipeline-overview.md) |
| `failure_mode` enum | [llm-agent-failure-handling.md § Failure taxonomy](llm-agent-failure-handling.md#failure-taxonomy) |
| `invocation_ended.status` enum | [llm-agent-failure-handling.md § Abort semantics](llm-agent-failure-handling.md#abort-semantics), [mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md), [command-center.md § Control surface](command-center.md#control-surface) (`skipped_paused`) |
| `agent_name` enum | [architecture/llm-integration.md](../architecture/llm-integration.md) |
| `profile_name` discipline | [configuration-management.md § Cross-reference](configuration-management.md) (an `active_profile` value names a file under `profiles/`) |
| Universe validation report shape | [asset-universe-validation.md § Validation procedure](asset-universe-validation.md#validation-procedure) |
| Emergency-invocation cooldown semantics | [breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) |

---

## Request and response schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/pipeline-control.json",
  "title": "Pipeline control surface",
  "description": "Request bodies and response envelopes for the pipeline's POST /control/* HTTP verbs.",

  "$defs": {

    "control_response_envelope": {
      "type": "object",
      "description": "Successful response envelope. Returned with HTTP 200 from every POST /control/* verb that does not carry verb-specific response data. Verbs that return verb-specific data extend this envelope with additional required fields.",
      "required": ["status", "applied_at"],
      "properties": {
        "status": {
          "type": "string",
          "enum": ["accepted"],
          "description": "Verb was applied. Idempotent verbs return accepted on no-op application as well (e.g., pause on already-paused returns accepted with the original applied_at unchanged)."
        },
        "applied_at": {
          "type": "string",
          "format": "date-time",
          "description": "When the pipeline applied the verb. For idempotent no-ops this is the timestamp of the original application, not of the current request."
        }
      }
    },

    "control_error_envelope": {
      "type": "object",
      "description": "Error response envelope. Returned with HTTP 4xx for caller errors (precondition_failed, validation_failed, not_found) and HTTP 5xx for internal errors (internal_error). The command-center backend renders code as the user-facing error class and detail as the human-readable explanation; details carries verb-specific structured fields where applicable.",
      "required": ["error"],
      "properties": {
        "error": {
          "type": "object",
          "required": ["code", "detail"],
          "properties": {
            "code": {
              "type": "string",
              "enum": [
                "precondition_failed",
                "validation_failed",
                "not_found",
                "cooldown_active",
                "internal_error"
              ]
            },
            "detail": {
              "type": "string",
              "description": "Human-readable explanation. Single sentence."
            },
            "details": {
              "type": "object",
              "description": "Verb-specific structured fields. Shape depends on the originating verb; documented in the per-verb table below."
            }
          }
        }
      }
    },

    "pause_request": {
      "type": "object",
      "required": ["reason"],
      "properties": {
        "reason": {
          "type": "string",
          "minLength": 1,
          "description": "Operator's reason for the pause. Persisted to activity_log alongside the operator-console source on the command-center side."
        }
      }
    },

    "resume_request": {
      "type": "object",
      "description": "No body. The verb is idempotent; resuming an already-running scheduler returns success unchanged.",
      "additionalProperties": false
    },

    "trigger_emergency_invocation_request": {
      "type": "object",
      "required": ["reason"],
      "properties": {
        "reason": {
          "type": "string",
          "minLength": 1,
          "description": "Operator's reason for forcing an out-of-schedule invocation."
        }
      }
    },

    "trigger_emergency_invocation_response": {
      "allOf": [
        { "$ref": "#/$defs/control_response_envelope" },
        {
          "type": "object",
          "required": ["invocation_id"],
          "properties": {
            "invocation_id": {
              "type": "string",
              "description": "ID assigned to the triggered emergency invocation. Use this to correlate with subsequent invocation_started / invocation_ended events on /events."
            }
          }
        }
      ]
    },

    "switch_profile_request": {
      "type": "object",
      "required": ["profile_name"],
      "properties": {
        "profile_name": {
          "type": "string",
          "minLength": 1,
          "description": "Profile to make active. Must name a file present in config/profiles/. The pipeline rewrites main.yaml's active_profile field; the change takes effect at the next invocation."
        }
      }
    },

    "run_universe_validation_request": {
      "type": "object",
      "description": "No body.",
      "additionalProperties": false
    },

    "run_universe_validation_response": {
      "allOf": [
        { "$ref": "#/$defs/control_response_envelope" },
        {
          "type": "object",
          "required": ["report"],
          "properties": {
            "report": {
              "type": "object",
              "description": "Inline result of scripts/validate_universe.py. Synchronous — the response only returns when validation has completed.",
              "required": ["validated_at", "tickers"],
              "properties": {
                "validated_at": { "type": "string", "format": "date-time" },
                "tickers": {
                  "type": "array",
                  "minItems": 1,
                  "items": { "$ref": "#/$defs/universe_validation_ticker_row" }
                }
              }
            }
          }
        }
      ]
    },

    "universe_validation_ticker_row": {
      "type": "object",
      "required": ["ticker", "verdict", "criteria"],
      "properties": {
        "ticker": { "type": "string" },
        "verdict": {
          "type": "string",
          "enum": ["pass", "fail", "unknown"],
          "description": "Aggregate verdict. fail when at least one criterion is fail; unknown when no criterion is fail and at least one is unknown; pass when every criterion is pass. Per asset-universe-validation.md § Validation procedure."
        },
        "criteria": {
          "type": "array",
          "minItems": 5,
          "maxItems": 5,
          "description": "One row per criterion in the order: adv, analyst_coverage, beta, market_cap, options_oi.",
          "items": { "$ref": "#/$defs/universe_validation_criterion_row" }
        }
      }
    },

    "universe_validation_criterion_row": {
      "type": "object",
      "required": ["criterion", "verdict"],
      "properties": {
        "criterion": {
          "type": "string",
          "enum": ["adv", "analyst_coverage", "beta", "market_cap", "options_oi"]
        },
        "verdict": {
          "type": "string",
          "enum": ["pass", "fail", "unknown"]
        },
        "computed_value": {
          "description": "The criterion's computed value. Shape depends on criterion (number for analyst_coverage, beta, market_cap, options_oi; object {shares, notional} for adv). Absent when verdict is unknown.",
          "oneOf": [
            { "type": "number" },
            {
              "type": "object",
              "required": ["shares", "notional"],
              "properties": {
                "shares": { "type": "number" },
                "notional": { "type": "number" }
              }
            }
          ]
        },
        "threshold": {
          "description": "The threshold the criterion was checked against. Same shape as computed_value for the criterion."
        },
        "note": {
          "type": "string",
          "description": "Present when verdict is unknown — explains the data-source failure (Polygon no data, Finnhub error)."
        }
      }
    }
  }
}
```

### Per-verb summary

| Verb | Request body | Success body | Error codes (HTTP) |
|---|---|---|---|
| `POST /control/pause` | `pause_request` | `control_response_envelope` | `validation_failed` (400) |
| `POST /control/resume` | `resume_request` | `control_response_envelope` | `internal_error` (500) |
| `POST /control/trigger_emergency_invocation` | `trigger_emergency_invocation_request` | `trigger_emergency_invocation_response` | `cooldown_active` (409, with `details: { cooldown_remaining_seconds: number, cooldown_started_at: date-time }`); `precondition_failed` (409, when an invocation is already running and `max_instances=1` blocks a new one, with `details: { running_invocation_id: string }`); `validation_failed` (400) |
| `POST /control/switch_profile` | `switch_profile_request` | `control_response_envelope` | `not_found` (404, when `profile_name` does not name a file under `config/profiles/`); `validation_failed` (400) |
| `POST /control/run_universe_validation` | `run_universe_validation_request` | `run_universe_validation_response` | `internal_error` (500, when `scripts/validate_universe.py` exits non-zero) |

---

## Event schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/pipeline-events.json",
  "title": "Pipeline event payloads",
  "description": "Per-event payload shapes for the SSE records emitted on GET /events. The data field of each SSE record validates against the matching $defs/<name>_event definition; the SSE event field carries the event name.",

  "oneOf": [
    { "$ref": "#/$defs/invocation_started_event" },
    { "$ref": "#/$defs/phase_transition_event" },
    { "$ref": "#/$defs/agent_started_event" },
    { "$ref": "#/$defs/agent_succeeded_event" },
    { "$ref": "#/$defs/agent_retrying_event" },
    { "$ref": "#/$defs/agent_failed_event" },
    { "$ref": "#/$defs/invocation_ended_event" },
    { "$ref": "#/$defs/next_trigger_changed_event" },
    { "$ref": "#/$defs/heartbeat_event" }
  ],

  "$defs": {

    "phase_enum": {
      "type": "string",
      "enum": ["collect", "distill", "analyze", "decide", "execute"],
      "description": "The pipeline's five sequential phases per pipeline-overview.md. collect drains the fill buffer; distill normalizes data and computes indicators; analyze runs domain researchers, qualitative, adaptive, and synthesizer; decide runs analyst, strategist, proposal pre-processor, PM; execute submits commands through the OMS."
    },

    "run_type_enum": {
      "type": "string",
      "enum": [
        "market_hours_rolling",
        "off_hours_rolling",
        "pre_open",
        "pre_close",
        "emergency"
      ],
      "description": "Trigger that fired the invocation. Per infrastructure.md § Trigger schedule plus the emergency trigger from breach-behavior.md."
    },

    "agent_name_enum": {
      "type": "string",
      "enum": [
        "domain_researcher_tech_semis",
        "domain_researcher_financials",
        "domain_researcher_energy",
        "qualitative_researcher",
        "adaptive_researcher",
        "synthesizer",
        "analyst",
        "strategist",
        "portfolio_manager"
      ],
      "description": "LLM agent identifier. The proposal pre-processor is deterministic and not an LLM agent; it does not emit agent_started / agent_succeeded events."
    },

    "failure_mode_enum": {
      "type": "string",
      "enum": [
        "timeout",
        "malformed_output",
        "context_overflow",
        "model_api_error",
        "tool_use_error"
      ],
      "description": "Per llm-agent-failure-handling.md § Failure taxonomy."
    },

    "invocation_started_event": {
      "type": "object",
      "required": ["invocation_id", "run_type", "started_at"],
      "properties": {
        "invocation_id": { "type": "string" },
        "run_type": { "$ref": "#/$defs/run_type_enum" },
        "started_at": { "type": "string", "format": "date-time" }
      }
    },

    "phase_transition_event": {
      "type": "object",
      "required": ["invocation_id", "phase", "phase_started_at"],
      "properties": {
        "invocation_id": { "type": "string" },
        "phase": { "$ref": "#/$defs/phase_enum" },
        "phase_started_at": { "type": "string", "format": "date-time" }
      }
    },

    "agent_started_event": {
      "type": "object",
      "required": ["invocation_id", "agent_name", "started_at", "latency_budget_seconds"],
      "properties": {
        "invocation_id": { "type": "string" },
        "agent_name": { "$ref": "#/$defs/agent_name_enum" },
        "started_at": { "type": "string", "format": "date-time" },
        "latency_budget_seconds": {
          "type": "number",
          "exclusiveMinimum": 0,
          "description": "Per-agent budget from the agent's deployment config — the value the live run watcher anchors its progress indicator on. See llm-agent-failure-handling.md § Detection."
        }
      }
    },

    "agent_succeeded_event": {
      "type": "object",
      "required": ["invocation_id", "agent_name", "duration_seconds", "tokens_used"],
      "properties": {
        "invocation_id": { "type": "string" },
        "agent_name": { "$ref": "#/$defs/agent_name_enum" },
        "duration_seconds": {
          "type": "number",
          "minimum": 0
        },
        "tokens_used": {
          "type": "object",
          "required": ["input", "output"],
          "properties": {
            "input": { "type": "integer", "minimum": 0 },
            "output": { "type": "integer", "minimum": 0 },
            "cache_read": { "type": "integer", "minimum": 0 },
            "cache_creation": { "type": "integer", "minimum": 0 }
          },
          "description": "Token accounting from the agent_calls record. cache_read and cache_creation present when prompt caching applied."
        }
      }
    },

    "agent_retrying_event": {
      "type": "object",
      "required": ["invocation_id", "agent_name", "attempt", "reason"],
      "properties": {
        "invocation_id": { "type": "string" },
        "agent_name": { "$ref": "#/$defs/agent_name_enum" },
        "attempt": {
          "type": "integer",
          "minimum": 2,
          "description": "Retry attempt number — the first invocation is attempt 1 and does not emit agent_retrying. Schema-repair retries fire with attempt=2 per llm-agent-failure-handling.md § Schema repair."
        },
        "reason": { "$ref": "#/$defs/failure_mode_enum" }
      }
    },

    "agent_failed_event": {
      "type": "object",
      "required": ["invocation_id", "agent_name", "failure_mode"],
      "properties": {
        "invocation_id": { "type": "string" },
        "agent_name": { "$ref": "#/$defs/agent_name_enum" },
        "failure_mode": { "$ref": "#/$defs/failure_mode_enum" }
      }
    },

    "invocation_ended_event": {
      "type": "object",
      "required": ["invocation_id", "status", "commands_issued"],
      "properties": {
        "invocation_id": { "type": "string" },
        "status": {
          "type": "string",
          "enum": ["completed", "failed", "partial", "skipped_paused"],
          "description": "completed = full execute phase ran; failed = aborted before execute; partial = execute ran but some downstream effect did not commit; skipped_paused = trigger fired while scheduler-pause flag was set per command-center.md § Control surface."
        },
        "commands_issued": {
          "type": "integer",
          "minimum": 0,
          "description": "Total OMS commands the execute phase submitted. Zero for skipped_paused, failed pre-execute, and rejection-only invocations."
        }
      }
    },

    "next_trigger_changed_event": {
      "type": "object",
      "required": ["next_trigger_at", "next_trigger_type"],
      "properties": {
        "next_trigger_at": {
          "type": "string",
          "format": "date-time"
        },
        "next_trigger_type": { "$ref": "#/$defs/run_type_enum" }
      }
    },

    "heartbeat_event": {
      "type": "object",
      "required": ["timestamp"],
      "properties": {
        "timestamp": { "type": "string", "format": "date-time" }
      }
    }
  }
}
```

### Per-event summary

| Event | Emitted when | Required fields |
|---|---|---|
| `invocation_started` | A pipeline invocation begins (any `run_type`, including `emergency`) | `invocation_id`, `run_type`, `started_at` |
| `phase_transition` | Pipeline enters a new phase | `invocation_id`, `phase`, `phase_started_at` |
| `agent_started` | An LLM agent begins | `invocation_id`, `agent_name`, `started_at`, `latency_budget_seconds` |
| `agent_succeeded` | An LLM agent returns valid output | `invocation_id`, `agent_name`, `duration_seconds`, `tokens_used` |
| `agent_retrying` | An LLM agent retries (schema repair, model API retry, etc.) | `invocation_id`, `agent_name`, `attempt`, `reason` |
| `agent_failed` | An LLM agent's retries are exhausted | `invocation_id`, `agent_name`, `failure_mode` |
| `invocation_ended` | A pipeline invocation reaches a terminal state | `invocation_id`, `status`, `commands_issued` |
| `next_trigger_changed` | APScheduler's next-trigger preview changes (pause, resume, emergency consumption, schedule edit) | `next_trigger_at`, `next_trigger_type` |
| `heartbeat` | 15 s elapsed since the last event of any kind on this connection | `timestamp` |

---

## Notes on cross-field invariants

Invariants enforced by the producer (pipeline) or consumer (command-center backend) but not expressible in JSON Schema:

- **`invocation_id` is consistent within an invocation.** Every `phase_transition`, `agent_started`, `agent_succeeded`, `agent_retrying`, `agent_failed`, and `invocation_ended` event for a given invocation carries the same `invocation_id` as the originating `invocation_started`. The command-center backend correlates downstream-rendered events by this ID.

- **Phase transitions are forward-only and cover the full pipeline.** `phase` advances strictly through the `phase_enum` order (`collect → distill → analyze → decide → execute`); a phase that failed mid-run does not emit a transition for the next phase, and `invocation_ended` follows with `status: failed` or `status: partial`. Skipping forward (`distill → decide`) or backward (`analyze → distill`) is a structural error.

- **Emergency triggers reuse the same invocation lifecycle.** `trigger_emergency_invocation` emits `invocation_started` with `run_type: emergency`; the rest of the lifecycle is identical to a scheduled invocation. The cooldown window described in [breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) is enforced by the verb's `cooldown_active` error envelope, not by event suppression.

- **`agent_succeeded` and `agent_failed` are mutually exclusive for a given `(invocation_id, agent_name)` pair.** Exactly one terminal event fires per agent per invocation — either succeeded after schema validation passes (possibly after a single corrective retry) or failed after retries exhaust per [llm-agent-failure-handling.md § Recovery semantics](llm-agent-failure-handling.md#recovery-semantics).

- **`agent_retrying.attempt` increases monotonically within an `(invocation_id, agent_name)` pair.** Caps at 2 for malformed-output (one corrective retry) and timeout (one retry with doubled budget); model-API-error retries follow the [api-failure-handling.md](01-data-layer/api-failure-handling.md) Critical-tier shape.

- **`invocation_ended.status: skipped_paused` carries `commands_issued: 0` and emits no `phase_transition`, `agent_*`, or `invocation_started` events for that trigger.** A paused trigger is logged at the scheduler layer; the SSE stream sees only `next_trigger_changed` (advancing past the skipped slot) and the operator's prior `pause` ack.

- **`run_type: emergency` invocations on `invocation_started` correlate with the `invocation_id` returned by `trigger_emergency_invocation_response`.** A consumer waiting for the lifecycle of a triggered emergency reads `invocation_id` from the verb response and filters events by that ID.

- **`tokens_used.cache_read + tokens_used.cache_creation ≤ tokens_used.input` when cache fields are present.** Cache reads and cache creation are both forms of input; the relationship matches the Anthropic SDK's prompt-caching accounting.

---

## Evolution

Wire-format changes land here first; prose updates in [command-center.md § Control surface](command-center.md#control-surface) and [§ Live event stream](command-center.md#live-event-stream) follow.

Adding a new pipeline event requires:

- A new entry in the `oneOf` and a matching `$defs/<name>_event` definition in this schema.
- A row in [command-center.md § Live event stream — Pipeline events](command-center.md#live-event-stream).
- A renderer in the command-center backend's downstream multiplexer and the frontend `EventSource` handler.

Adding a new control verb requires:

- A new `$defs/<verb>_request` and (if non-empty response) `<verb>_response` definition.
- A row in [command-center.md § Control surface — Pipeline endpoints](command-center.md#control-surface) and in this doc's per-verb summary.
- A proxy route on the command-center backend's `/api/control/*` surface with auth, audit, and the operator-console source tag.

The cooldown_active error code is load-bearing for the emergency-invocation trigger's interaction with [breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger). If the cooldown semantics change (e.g., per-rule cooldowns), the `details` shape on cooldown_active changes in coordination.

---

## Cross-references

- Prose contract for pipeline endpoints and pipeline events: [command-center.md § Control surface](command-center.md#control-surface), [§ Live event stream](command-center.md#live-event-stream)
- Sibling wire-format schema (continuous monitor): [monitor-control-and-events-schema.md](monitor-control-and-events-schema.md)
- Pipeline phase definitions: [pipeline-overview.md](pipeline-overview.md)
- Trigger schedule and run-type taxonomy: [infrastructure.md § Trigger schedule](../architecture/infrastructure.md)
- LLM agent inventory and orchestration: [llm-integration.md](../architecture/llm-integration.md)
- Failure taxonomy and retry semantics: [llm-agent-failure-handling.md](llm-agent-failure-handling.md)
- Mid-pipeline failure handling (status transitions): [mid-pipeline-failure-handling.md](mid-pipeline-failure-handling.md)
- Universe validation procedure and report shape: [asset-universe-validation.md](asset-universe-validation.md)
- Profile catalog and active_profile discipline: [configuration-management.md](configuration-management.md), [rules-and-limits.md § Portfolio size profiles](06-risk-guardrails/rules-and-limits.md#portfolio-size-profiles)
- Emergency invocation cooldown: [breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger)
