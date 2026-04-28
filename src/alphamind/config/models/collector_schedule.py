"""Pydantic models for collector_schedule.yaml."""

import re

from pydantic import BaseModel, field_validator

# 5-field cron: each field is digits/commas/dashes/slashes/stars, or day-of-week abbrevs
_CRON_FIELD_RE = re.compile(
    r"^[0-9,\-*/]+$"
    r"|^[a-zA-Z]{3}(-[a-zA-Z]{3})?(,[a-zA-Z]{3}(-[a-zA-Z]{3})?)*$"
)


def _validate_cron(cron: str) -> str:
    fields = cron.split()
    if len(fields) != 5:
        msg = f"Cron expression must have exactly 5 fields, got {len(fields)}: {cron!r}"
        raise ValueError(msg)
    for field in fields:
        if not _CRON_FIELD_RE.match(field):
            msg = f"Invalid cron field {field!r} in expression {cron!r}"
            raise ValueError(msg)
    return cron


class CollectorEntry(BaseModel):
    cron: str

    @field_validator("cron")
    @classmethod
    def cron_is_valid(cls, v: str) -> str:
        return _validate_cron(v)


class CollectorScheduleConfig(BaseModel):
    timezone: str
    collectors: dict[str, CollectorEntry]
