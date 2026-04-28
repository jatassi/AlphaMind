"""Pydantic models for main.yaml — composition root (story 03b)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ExecutionMode(StrEnum):
    paper = "paper"
    live = "live"


class Profile(StrEnum):
    micro = "micro"
    small = "small"
    medium = "medium"
    large = "large"


class Paths(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: str
    logs: str
    archive: str
    prompts: str


class MainConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    active_profile: Profile
    execution_mode: ExecutionMode
    paths: Paths
