---
status: not_started
completed_date:
commit_id:
---

# 03g — `llm_failure.yaml` + Pydantic model

## Goal

Land `config/llm_failure.yaml` (per-failure-mode retry policy for every LLM agent invocation in the pipeline) and a Pydantic model. The file is the operator-tunable surface of `llm-agent-failure-handling.md`'s fail-closed policy: each failure mode has bounded retry behavior the pipeline applies before aborting the invocation.

## Reading

- `docs/design/configuration-management.md` § `llm_failure.yaml` — schema and worked example
- `docs/design/llm-agent-failure-handling.md` — the five failure modes and their retry shapes (canonical specification)
- `docs/design/llm-agent-failure-handling.md` § Schema-repair retry — the `same_context_corrective` strategy contract
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/llm_failure.yaml` populated with the design-doc worked example:
  - `retries.model_api_error: { attempts: 3, backoff: exponential }`
  - `retries.timeout: { attempts: 1, strategy: doubled_latency_budget }`
  - `retries.malformed_output: { attempts: 1, strategy: same_context_corrective }`
  - `retries.context_overflow: { attempts: 0 }`
  - `retries.tool_use_error: { attempts: 1, condition: idempotent_only }`
- `src/alphamind/config/models/llm_failure.py` defining:
  - `FailureMode` (StrEnum: `model_api_error`, `timeout`, `malformed_output`, `context_overflow`, `tool_use_error` — the five modes from `llm-agent-failure-handling.md`)
  - `BackoffStrategy` (StrEnum: `exponential`, `none`) — disjoint from the `BackoffStrategy` in `models/data_sources.py`; **do not import from data_sources** to keep the LLM module self-contained, but use the same string values
  - `RetryStrategy` (StrEnum: `doubled_latency_budget`, `same_context_corrective`) — the named strategies from the design doc
  - `RetryCondition` (StrEnum: `idempotent_only`)
  - `RetryPolicy` (BaseModel: `attempts: int = Field(ge=0)`, `backoff: BackoffStrategy | None = None`, `strategy: RetryStrategy | None = None`, `condition: RetryCondition | None = None`)
  - `LLMFailureConfig` (BaseModel: `retries: dict[FailureMode, RetryPolicy]`)
- A model validator on `LLMFailureConfig` enforcing:
  - Every member of `FailureMode` is a key in `retries:`. Missing modes raise.
  - Per-mode field constraints (encoded as a per-mode allow-list rather than ad-hoc field validators):
    - `model_api_error`: requires `backoff`; rejects `strategy`, `condition`
    - `timeout`: requires `strategy`; rejects `backoff`, `condition`
    - `malformed_output`: requires `strategy`; rejects `backoff`, `condition`
    - `context_overflow`: requires `attempts == 0`; rejects `backoff`, `strategy`, `condition`
    - `tool_use_error`: requires `condition`; rejects `backoff`, `strategy`
- Re-export `LLMFailureConfig`, `RetryPolicy`, `FailureMode`, `BackoffStrategy`, `RetryStrategy`, `RetryCondition` from `models/__init__.py`.
- Unit tests covering: shipped `config/llm_failure.yaml` parses cleanly; missing failure mode raises; `context_overflow.attempts: 1` raises; a `model_api_error` entry without `backoff` raises; a `tool_use_error` entry with `backoff` declared raises (extra field rejected per the per-mode constraint).

Out of scope:
- Coupling between `attempts` and the runtime retry executor — the model carries the policy, the pipeline applies it.
- Wiring `LLMFailureConfig` into the loader aggregate (story 08).
- Per-agent retry overrides — the design doc specifies a single global policy, not per-agent.

## Notes

The design doc treats the five failure modes as a **closed set**. Adding a new failure mode requires editing both `llm-agent-failure-handling.md` and the `FailureMode` enum, in that order. The model validator enforcing closure (every enum member is a key) catches any operator who edits the YAML before the spec.

`context_overflow.attempts == 0` is a hard fail-closed: there is no retry path that helps when an agent's input bundle exceeds its model's context window. The validator must reject any positive value to prevent operator drift.

The `BackoffStrategy.none` member exists in the data-sources model for the `optional` retry shape; including it here gives operators a uniform vocabulary even though no LLM failure mode currently uses `none`. **Do not** share the enum import — duplicate the StrEnum definition in this module so changes to one don't accidentally land in the other.

Per-mode field constraints are best expressed as a `model_validator(mode="after")` that switches on the mode and asserts presence/absence of each optional field. The error message must name the violating mode so operators can find it in the YAML. Ad-hoc separate validators per field are harder to test exhaustively.

`RetryPolicy` carries every optional field as `None` by default. The model accepts a YAML entry with only `attempts` set (e.g., `context_overflow: { attempts: 0 }`); the per-mode validator then enforces which optional fields must or must not be present.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/llm_failure.yaml` exists, declares a `retries:` map with entries for all five failure modes and the per-mode fields shown in Scope.
- [ ] `config/llm_failure.yaml` parses cleanly via `yaml.safe_load` and validates against `LLMFailureConfig`.
- [ ] `src/alphamind/config/models/llm_failure.py` defines all six models/enums listed in Scope.
- [ ] `models/__init__.py` re-exports the six names.
- [ ] A unit test asserts the shipped `config/llm_failure.yaml` parses and the resulting `LLMFailureConfig` exposes all five failure modes.
- [ ] A unit test asserts a YAML missing the `tool_use_error` entry raises `ValidationError`.
- [ ] A unit test asserts `context_overflow: { attempts: 1 }` raises `ValidationError`.
- [ ] A unit test asserts `model_api_error: { attempts: 3 }` (no `backoff`) raises `ValidationError`.
- [ ] A unit test asserts `tool_use_error: { attempts: 1, condition: idempotent_only, backoff: exponential }` raises `ValidationError`.
- [ ] A unit test asserts `timeout: { attempts: 1, strategy: same_context_corrective }` is accepted (string-equal to a known `RetryStrategy` member, even if the design doc only canonically pairs `same_context_corrective` with `malformed_output`).
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
