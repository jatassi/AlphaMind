"""Pydantic models for llm_failure.yaml.

Per-failure-mode retry policy applied before any LLM agent invocation aborts.
The five failure modes are the closed set defined in
`docs/design/llm-agent-failure-handling.md`; adding a mode requires editing
both the design doc and the `FailureMode` enum.

The `BackoffStrategy` here intentionally duplicates the StrEnum in
`models/data_sources.py` (same string values, separate class) to keep the LLM
module self-contained — changes to one must not silently land in the other.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FailureMode(StrEnum):
    model_api_error = "model_api_error"
    timeout = "timeout"
    malformed_output = "malformed_output"
    context_overflow = "context_overflow"
    tool_use_error = "tool_use_error"


class BackoffStrategy(StrEnum):
    exponential = "exponential"
    none = "none"


class RetryStrategy(StrEnum):
    doubled_latency_budget = "doubled_latency_budget"
    same_context_corrective = "same_context_corrective"


class RetryCondition(StrEnum):
    idempotent_only = "idempotent_only"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempts: int = Field(ge=0)
    backoff: BackoffStrategy | None = None
    strategy: RetryStrategy | None = None
    condition: RetryCondition | None = None


# Per-mode allow-list: the optional `RetryPolicy` field that must be present
# for each failure mode. `None` means no optional field is permitted —
# `attempts` is the only field carried.
_REQUIRED_FIELD_BY_MODE: dict[FailureMode, str | None] = {
    FailureMode.model_api_error: "backoff",
    FailureMode.timeout: "strategy",
    FailureMode.malformed_output: "strategy",
    FailureMode.context_overflow: None,
    FailureMode.tool_use_error: "condition",
}

_OPTIONAL_RETRY_FIELDS: frozenset[str] = frozenset(
    name for name, info in RetryPolicy.model_fields.items() if not info.is_required()
)


class LLMFailureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    retries: dict[FailureMode, RetryPolicy]

    @model_validator(mode="after")
    def all_failure_modes_present(self) -> "LLMFailureConfig":
        missing = set(FailureMode) - set(self.retries.keys())
        if missing:
            raise ValueError(
                f"retries: missing entries for failure modes {sorted(m.value for m in missing)}"
            )
        return self

    @model_validator(mode="after")
    def per_mode_field_constraints(self) -> "LLMFailureConfig":
        for mode, policy in self.retries.items():
            required = _REQUIRED_FIELD_BY_MODE[mode]
            for field in _OPTIONAL_RETRY_FIELDS:
                value = getattr(policy, field)
                if field == required and value is None:
                    raise ValueError(
                        f"retries.{mode.value}: field {field!r} is required but was not provided"
                    )
                if field != required and value is not None:
                    raise ValueError(
                        f"retries.{mode.value}: field {field!r} is not "
                        f"permitted for this failure mode"
                    )

            if mode is FailureMode.context_overflow and policy.attempts != 0:
                raise ValueError(
                    f"retries.{mode.value}: attempts must be 0 (no retry path "
                    f"helps when input exceeds the model context window)"
                )
        return self
