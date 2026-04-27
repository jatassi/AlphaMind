# Monitor control and events schema

Formal JSON Schema (Draft 2020-12) for the continuous monitor process's loopback HTTP surface — request bodies and response envelopes for `POST /control/*`, plus the per-event payloads emitted on the `GET /events` SSE stream. Machine-readable counterpart to [command-center.md § Control surface](command-center.md#control-surface) and [§ Live event stream](command-center.md#live-event-stream). The command-center backend validates every request body before forwarding and every event payload before re-emitting downstream.

Pipeline wire format is specified separately in [pipeline-control-and-events-schema.md](pipeline-control-and-events-schema.md) — pipeline and continuous monitor are distinct producers of distinct event taxonomies.

## Scope

- **Producer:** the continuous monitor process per [05-execution-layer/architecture.md §4](05-execution-layer/architecture.md). FastAPI + Uvicorn bound to `127.0.0.1` within the same OS process that runs the websocket consumer, breach detector, greeks refresh loop, and options bracket evaluator. The command-center backend is the only client; loopback isolation is the trust boundary.
- **Surface:** three `POST /control/*` verbs (cancel order, force-close position, set halt mode) plus one `GET /events` SSE stream carrying seven event types (six operational, one heartbeat).
- **What is and is not in scope:** request bodies, success response envelopes, error response envelope, SSE framing, and per-event payload shapes are in scope. Authentication is not — the surface is loopback-bound and the public-facing `/api/control/*` proxy on the command-center backend owns auth and audit per [command-center.md § Control surface](command-center.md#control-surface). HTTP status code semantics follow standard HTTP and FastAPI conventions and are noted in the per-verb tables; the schemas describe body shapes only.
- **Header conventions:** `Content-Type: application/json` on every control request and response with a JSON body. `Content-Type: text/event-stream` with `Cache-Control: no-cache` and `Connection: keep-alive` on the `GET /events` response. No request-id or correlation header — the command-center backend is the only client and owns its own correlation in the audit trail.
- **SSE framing.** Each event is emitted as one SSE record terminated by a blank line:

  ```
  event: <event_name>
  data: <json>

  ```

  The `event` field carries the event name from the table in [Monitor events](#monitor-events); the `data` field carries the event payload as a single-line JSON document conforming to the matching `$defs/<name>_event` definition. The `id` SSE field is not emitted — monitor events have no historical guarantee per [command-center.md § Live event stream](command-center.md#live-event-stream); browsers reconnect fresh and there is no `Last-Event-ID` resume.

- **Liveness:** the `heartbeat` event fires every 15 seconds when no other event has been emitted, so a healthy connection always has a record within the last 15 seconds. The command-center backend's reconnect logic uses idle-timeout > 30 seconds as its disconnect signal.

- **Boundary with the OMS:** `cancel_order` and `force_close_position` route through the monitor because the monitor owns the engine-originated envelope path per [05-execution-layer/architecture.md §4b](05-execution-layer/architecture.md). The verbs synthesize an engine-originated envelope per [engine-envelope-schema.md](05-execution-layer/engine-envelope-schema.md) and submit it to the OMS the same way mechanical breach responses do; the only difference is the envelope's `position_selection_rationale` carries operator-console attribution.

## Cross-references

Enum values and structural constraints trace back to these authoritative sources:

| Field | Source |
|---|---|
| `POST /control/*` verb set | [command-center.md § Control surface — Monitor endpoints](command-center.md#control-surface) |
| Event name set | [command-center.md § Live event stream — Monitor events](command-center.md#live-event-stream) |
| `force_close_position` envelope shape | [engine-envelope-schema.md](05-execution-layer/engine-envelope-schema.md), [oms-commands.md — CLOSE](05-execution-layer/oms-commands.md#close) |
| `force_close_position` rationale subtype | [pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md) (`risk_management_subtype: pm_directed`) |
| `breach_detected.rule` values | [rules-and-limits.md](06-risk-guardrails/rules-and-limits.md) |
| `breach_detected.response_classification` values | [breach-behavior.md § Per-rule breach response classification](06-risk-guardrails/breach-behavior.md#per-rule-breach-response-classification) |
| `fill_received` field semantics | [05-execution-layer/architecture.md § Fill report contract](05-execution-layer/architecture.md#fill-report-contract) |
| `greeks_refreshed` triggers | [05-execution-layer/architecture.md §4d](05-execution-layer/architecture.md) |
| `emergency_invocation_triggered` triggers | [breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) |
| `set_halt_mode` semantics | [breach-behavior.md § Drawdown halt mode](06-risk-guardrails/breach-behavior.md#drawdown-halt-mode) |

---

## Request and response schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/monitor-control.json",
  "title": "Continuous monitor control surface",
  "description": "Request bodies and response envelopes for the monitor's POST /control/* HTTP verbs.",

  "$defs": {

    "control_response_envelope": {
      "type": "object",
      "description": "Successful response envelope. Returned with HTTP 200 from every POST /control/* verb that does not carry verb-specific response data. Verbs that return verb-specific data extend this envelope with additional required fields.",
      "required": ["status", "applied_at"],
      "properties": {
        "status": {
          "type": "string",
          "enum": ["accepted"],
          "description": "Verb was applied. Idempotent verbs return accepted on no-op application as well (e.g., set_halt_mode with the current state returns accepted with the original applied_at unchanged)."
        },
        "applied_at": {
          "type": "string",
          "format": "date-time",
          "description": "When the monitor applied the verb. For idempotent no-ops this is the timestamp of the original application, not of the current request."
        }
      }
    },

    "control_error_envelope": {
      "type": "object",
      "description": "Error response envelope. Returned with HTTP 4xx for caller errors (precondition_failed, validation_failed, not_found) and HTTP 5xx for internal errors (internal_error, broker_error). The command-center backend renders code as the user-facing error class and detail as the human-readable explanation; details carries verb-specific structured fields where applicable.",
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
                "broker_error",
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

    "cancel_order_request": {
      "type": "object",
      "required": ["order_id"],
      "properties": {
        "order_id": {
          "type": "string",
          "description": "OMS client_order_id of a pending order. Must match a row in the orders table whose status is open or partially_filled. The monitor synthesizes an engine-originated CANCEL envelope routed through the OMS."
        }
      }
    },

    "force_close_position_request": {
      "type": "object",
      "required": ["position_id", "rationale"],
      "properties": {
        "position_id": {
          "type": "string",
          "description": "Open position to close. Must match a row in the positions table whose status is open."
        },
        "rationale": {
          "type": "string",
          "minLength": 1,
          "description": "Operator's free-text rationale. Persisted on the synthesized engine-originated CLOSE envelope's guardrail_trigger_record.position_selection_rationale field, prefixed with operator-console attribution."
        }
      }
    },

    "force_close_position_response": {
      "allOf": [
        { "$ref": "#/$defs/control_response_envelope" },
        {
          "type": "object",
          "required": ["envelope_id"],
          "properties": {
            "envelope_id": {
              "type": "string",
              "description": "ID of the engine-originated envelope the verb synthesized. Format per oms-command-ids.md — Engine-originated command IDs (MON.{monitor_session_id}.{trigger_id}). Use this to correlate with subsequent fill_received events on /events and with the activity_log row written by the OMS."
            }
          }
        }
      ]
    },

    "set_halt_mode_request": {
      "type": "object",
      "required": ["enabled", "reason"],
      "properties": {
        "enabled": {
          "type": "boolean",
          "description": "true engages halt mode (no new positions, defensive-posture agent behavior); false lifts it. Idempotent — setting to the current state returns accepted unchanged."
        },
        "reason": {
          "type": "string",
          "minLength": 1,
          "description": "Operator's reason for the toggle. Persisted to the activity_log alongside the operator-console source on the command-center side."
        }
      }
    }
  }
}
```

### Per-verb summary

| Verb | Request body | Success body | Error codes (HTTP) |
|---|---|---|---|
| `POST /control/cancel_order` | `cancel_order_request` | `control_response_envelope` | `not_found` (404, when `order_id` does not match an order); `precondition_failed` (409, when the order is already filled, cancelled, or expired, with `details: { current_status: string }`); `broker_error` (502, when the broker adapter rejects the cancel request, with `details: { broker_message: string }`); `validation_failed` (400) |
| `POST /control/force_close_position` | `force_close_position_request` | `force_close_position_response` | `not_found` (404, when `position_id` does not match a position); `precondition_failed` (409, when the position is already closed, with `details: { current_status: string }`); `broker_error` (502, with `details: { broker_message: string }`); `validation_failed` (400) |
| `POST /control/set_halt_mode` | `set_halt_mode_request` | `control_response_envelope` | `validation_failed` (400) |

---

## Event schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://alphamind.local/schemas/monitor-events.json",
  "title": "Continuous monitor event payloads",
  "description": "Per-event payload shapes for the SSE records emitted on GET /events. The data field of each SSE record validates against the matching $defs/<name>_event definition; the SSE event field carries the event name.",

  "oneOf": [
    { "$ref": "#/$defs/websocket_connected_event" },
    { "$ref": "#/$defs/websocket_disconnected_event" },
    { "$ref": "#/$defs/fill_received_event" },
    { "$ref": "#/$defs/breach_detected_event" },
    { "$ref": "#/$defs/emergency_invocation_triggered_event" },
    { "$ref": "#/$defs/greeks_refreshed_event" },
    { "$ref": "#/$defs/heartbeat_event" }
  ],

  "$defs": {

    "websocket_connected_event": {
      "type": "object",
      "required": ["timestamp"],
      "properties": {
        "timestamp": {
          "type": "string",
          "format": "date-time",
          "description": "When the trade_updates websocket connected. After a disconnect-reconnect cycle, this is the new connection's establishment timestamp."
        }
      }
    },

    "websocket_disconnected_event": {
      "type": "object",
      "required": ["timestamp", "reason"],
      "properties": {
        "timestamp": {
          "type": "string",
          "format": "date-time"
        },
        "reason": {
          "type": "string",
          "description": "Disconnect reason. Free-text per the underlying websocket library — common values include network_error, server_close, idle_timeout. The frontend renders this verbatim under the websocket-state pane."
        }
      }
    },

    "fill_received_event": {
      "type": "object",
      "required": ["order_id", "position_id", "fill_price", "fill_qty"],
      "properties": {
        "order_id": {
          "type": "string",
          "description": "OMS client_order_id of the order this fill belongs to. Per 05-execution-layer/architecture.md § Fill report contract."
        },
        "position_id": {
          "type": "string",
          "description": "Position the fill applies to. Resolved by the monitor at fill receipt before the event emits."
        },
        "fill_price": {
          "type": "number",
          "exclusiveMinimum": 0
        },
        "fill_qty": {
          "type": "number",
          "description": "Shares (equity) or contracts (options) filled. May be partial — if a follow-up fill arrives, a separate fill_received event emits for it."
        }
      }
    },

    "breach_detected_event": {
      "type": "object",
      "required": ["rule", "current_value", "limit", "response_classification"],
      "properties": {
        "rule": {
          "type": "string",
          "description": "Guardrail rule that breached. Values come from rules-and-limits.md — examples include per_position_max_size, sector_concentration, daily_drawdown, cumulative_drawdown, margin_call, position_level_max_loss, total_short_exposure, single_short_max_size."
        },
        "current_value": { "type": "number" },
        "limit": { "type": "number" },
        "response_classification": {
          "type": "string",
          "enum": ["immediate", "deferred"],
          "description": "How the monitor handled the breach. immediate = monitor issued a protective CLOSE via an engine-originated envelope (or engaged halt mode for daily drawdown); deferred = breach logged for the strategist/PM at the next invocation. Per breach-behavior.md § Per-rule breach response classification."
        }
      }
    },

    "emergency_invocation_triggered_event": {
      "type": "object",
      "required": ["reason"],
      "properties": {
        "reason": {
          "type": "string",
          "description": "Trigger condition. Values come from breach-behavior.md § Emergency invocation trigger — examples include regime_jump, multi_rule_breach, rapid_drawdown_acceleration, margin_call. The monitor fires a corresponding pipeline POST /control/trigger_emergency_invocation; the pipeline emits its own invocation_started event when the invocation begins."
        }
      }
    },

    "greeks_refreshed_event": {
      "type": "object",
      "required": ["underlying", "refreshed_at"],
      "properties": {
        "underlying": {
          "type": "string",
          "description": "Underlying ticker whose option positions had greeks refreshed. One event per refresh cycle per underlying."
        },
        "refreshed_at": { "type": "string", "format": "date-time" }
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
| `websocket_connected` | The Alpaca `trade_updates` websocket establishes a connection | `timestamp` |
| `websocket_disconnected` | The websocket drops or closes | `timestamp`, `reason` |
| `fill_received` | The monitor writes a fill to the buffer (after Alpaca's `trade_updates` event) | `order_id`, `position_id`, `fill_price`, `fill_qty` |
| `breach_detected` | The breach detector evaluates a rule as breached against live state | `rule`, `current_value`, `limit`, `response_classification` |
| `emergency_invocation_triggered` | The monitor fires `POST /control/trigger_emergency_invocation` on the pipeline | `reason` |
| `greeks_refreshed` | A scheduled or move-based greeks refresh completes for one underlying | `underlying`, `refreshed_at` |
| `heartbeat` | 15 s elapsed since the last event of any kind on this connection | `timestamp` |

---

## Notes on cross-field invariants

Invariants enforced by the producer (monitor) or consumer (command-center backend) but not expressible in JSON Schema:

- **`websocket_connected` and `websocket_disconnected` alternate.** Two consecutive `websocket_connected` events without an intervening `websocket_disconnected` (or vice versa) is a structural error. The command-center backend uses this invariant to render the time-since-connect counter without polling.

- **`fill_received` references an order known to the OMS.** `order_id` matches a `client_order_id` written by an earlier OPEN, ADD, ADJUST, or CLOSE command; `position_id` matches the position the order targets. The monitor resolves these before emitting; the event never carries an unresolved order or position ID.

- **`breach_detected.response_classification: immediate` is followed by a corresponding engine-originated CLOSE envelope on the OMS side.** The envelope conforms to [engine-envelope-schema.md](05-execution-layer/engine-envelope-schema.md); its `guardrail_trigger_record.rule_breached` matches the `rule` field of this event, and its `breach_details` mirrors `current_value`, `limit`, and the breach overage. The two records (event payload here, envelope on the OMS side) are correlated by trigger timestamp and rule.

- **`breach_detected.response_classification: deferred` produces no engine-originated envelope.** The breach is recorded in the activity log for the strategist's next invocation; no protective CLOSE fires from the monitor.

- **`emergency_invocation_triggered` is followed by a pipeline `invocation_started` event with `run_type: emergency`** unless the pipeline rejects the trigger via its `cooldown_active` or `precondition_failed` error envelope (see [pipeline-control-and-events-schema.md § Per-verb summary](pipeline-control-and-events-schema.md#per-verb-summary)). The two events emit on separate SSE streams; the command-center backend correlates them downstream.

- **`force_close_position_response.envelope_id` matches the `envelope_id` field of the synthesized engine-originated envelope.** Per [engine-envelope-schema.md](05-execution-layer/engine-envelope-schema.md) the format is `MON.{monitor_session_id}.{trigger_id}`. The trigger ID portion increments monotonically within a monitor session; a session restart resets it.

- **Halt-mode state is monitor-local and persists across `set_halt_mode` toggles.** The flag does not auto-reset on monitor restart — restart preserves the last-set value via the `halt_mode_engaged` portfolio state field. A `set_halt_mode` with the current value returns `accepted` with the original `applied_at` rather than overwriting.

- **`greeks_refreshed.underlying` matches a ticker with at least one open option position at refresh time.** The monitor scopes refreshes to underlyings with open option exposure per [05-execution-layer/architecture.md §4d](05-execution-layer/architecture.md); a refresh against an underlying with no exposure is suppressed.

---

## Evolution

Wire-format changes land here first; prose updates in [command-center.md § Control surface](command-center.md#control-surface) and [§ Live event stream](command-center.md#live-event-stream) follow.

Adding a new monitor event requires:

- A new entry in the `oneOf` and a matching `$defs/<name>_event` definition in this schema.
- A row in [command-center.md § Live event stream — Monitor events](command-center.md#live-event-stream).
- A renderer in the command-center backend's downstream multiplexer and the frontend `EventSource` handler.

Adding a new control verb requires:

- A new `$defs/<verb>_request` and (if non-empty response) `<verb>_response` definition.
- A row in [command-center.md § Control surface — Monitor endpoints](command-center.md#control-surface) and in this doc's per-verb summary.
- A proxy route on the command-center backend's `/api/control/*` surface with auth, audit, and the operator-console source tag.
- For verbs that synthesize engine-originated envelopes (`cancel_order`, `force_close_position`, plus any future order/position-mutating verb): an explicit mapping in the verb's implementation onto [engine-envelope-schema.md](05-execution-layer/engine-envelope-schema.md), with the rationale subtype set to `pm_directed` per the operator-console attribution discipline already in [pm-envelope-schema.md](04-decision-layer/pm-envelope-schema.md).

The `breach_detected.rule` field is intentionally typed as a free string keyed to [rules-and-limits.md](06-risk-guardrails/rules-and-limits.md) rather than enumerated here — the rule registry lives in `config/guardrails.yaml` and adding a rule does not need a coordinated schema bump. The `response_classification` field stays enumerated because adding a new value (e.g., a third class between immediate and deferred) is a behavioral change, not a configuration change, and warrants explicit schema evolution.

---

## Cross-references

- Prose contract for monitor endpoints and monitor events: [command-center.md § Control surface](command-center.md#control-surface), [§ Live event stream](command-center.md#live-event-stream)
- Sibling wire-format schema (pipeline): [pipeline-control-and-events-schema.md](pipeline-control-and-events-schema.md)
- Continuous monitor responsibilities: [05-execution-layer/architecture.md §4](05-execution-layer/architecture.md)
- Engine-originated envelope format (target of cancel_order and force_close_position): [engine-envelope-schema.md](05-execution-layer/engine-envelope-schema.md)
- Engine-originated command ID format: [oms-command-ids.md — Engine-originated command IDs](oms-command-ids.md#engine-originated-command-ids)
- Fill report contract (source of fill_received fields): [05-execution-layer/architecture.md § Fill report contract](05-execution-layer/architecture.md#fill-report-contract)
- Breach response classification: [breach-behavior.md § Per-rule breach response classification](06-risk-guardrails/breach-behavior.md#per-rule-breach-response-classification)
- Halt mode semantics: [breach-behavior.md § Drawdown halt mode](06-risk-guardrails/breach-behavior.md#drawdown-halt-mode)
- Emergency invocation trigger: [breach-behavior.md § Emergency invocation trigger](06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger)
- Rule registry (source of breach_detected.rule values): [rules-and-limits.md](06-risk-guardrails/rules-and-limits.md)
