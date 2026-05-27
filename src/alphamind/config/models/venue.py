"""Pydantic models for venue.yaml — Alpaca venue parameters (story 03d).

The file is the broker-adapter's source of truth for paper/live URLs,
API-key environment-variable references, rate limits, and equity-session
windows. Settlement rules, PDT thresholds, and Reg T margin tiers live
in code per ``configuration-management.md § In code``.

The ``live`` block is always present — even on a paper-only checkout —
so a runtime switch of ``execution_mode`` to ``live`` surfaces missing
env-var values rather than a missing config block.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alphamind.config.models._shared import validate_hh_mm as _validate_hh_mm

# Names of environment variables follow the standard convention:
# uppercase first letter, then uppercase / digits / underscores.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _validate_env_var_name(value: str) -> str:
    if not _ENV_VAR_NAME_RE.match(value):
        msg = (
            f"Env-var name {value!r} must match {_ENV_VAR_NAME_RE.pattern} "
            f"(uppercase start, then uppercase/digit/underscore)"
        )
        raise ValueError(msg)
    return value


class AlpacaCredentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    rest_url: str
    ws_url: str
    api_key_env: str
    api_secret_env: str

    @field_validator("rest_url")
    @classmethod
    def rest_url_is_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            msg = f"rest_url {value!r} must start with 'https://'"
            raise ValueError(msg)
        return value

    @field_validator("ws_url")
    @classmethod
    def ws_url_is_wss(cls, value: str) -> str:
        if not value.startswith("wss://"):
            msg = f"ws_url {value!r} must start with 'wss://'"
            raise ValueError(msg)
        return value

    @field_validator("api_key_env", "api_secret_env")
    @classmethod
    def env_var_name_well_formed(cls, value: str) -> str:
        return _validate_env_var_name(value)


class Alpaca(BaseModel):
    model_config = ConfigDict(frozen=True)

    paper: AlpacaCredentials
    live: AlpacaCredentials
    rate_limit_per_minute: int = Field(ge=1)


class SessionWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    open: str
    close: str

    @field_validator("open", "close")
    @classmethod
    def hh_mm_well_formed(cls, value: str) -> str:
        return _validate_hh_mm(value)


class SessionHours(BaseModel):
    model_config = ConfigDict(frozen=True)

    regular: SessionWindow
    pre_market: SessionWindow
    after_hours: SessionWindow


class VenueConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    alpaca: Alpaca
    session_hours: SessionHours
