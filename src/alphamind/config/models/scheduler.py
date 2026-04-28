"""Pydantic models for scheduler.yaml (story 03c)."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alphamind.config.models.collector_schedule import _validate_cron

# Trigger keys are stems for run_types/<key>.yaml — restrict to snake_case
# so the resolver can build the filename without escaping or normalization.
_TRIGGER_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    timezone: str
    max_instances: int = Field(ge=1)
    overlap_dedup_lookback_minutes: int = Field(ge=0)
    triggers: dict[str, str]

    @field_validator("triggers")
    @classmethod
    def triggers_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("triggers map must not be empty")
        for key, cron in value.items():
            if not _TRIGGER_KEY_RE.match(key):
                msg = (
                    f"Trigger key {key!r} must match snake_case pattern "
                    f"^[a-z][a-z0-9_]*$ (used as run_types/<key>.yaml stem)"
                )
                raise ValueError(msg)
            _validate_cron(cron)
        return value
