"""Configuration for the state-persistence layer.

Owns the per-feature knobs the persistence layer reads at invocation start:
the sliding-window size for the recent-PM-decision read path, the snapshot-
isolation read timeout, and the filesystem roots for the per-process and
per-invocation provenance snapshots.

The config object itself is frozen — downstream stories receive an immutable
handle and store no copies. Mutation attempts raise ``pydantic.ValidationError``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from alphamind.execution.corporate_actions.config import CorporateActionsConfig


class StatePersistenceConfig(BaseModel):
    """Operator-tunable knobs for the state-persistence primitives."""

    model_config = ConfigDict(frozen=True, strict=True)

    pm_decision_log_sliding_window_invocations: Annotated[int, Field(ge=1)]
    snapshot_read_timeout_seconds: Annotated[float, Field(gt=0.0)]
    pip_freeze_snapshot_root: str
    invocation_provenance_root: str
    corporate_actions: CorporateActionsConfig = CorporateActionsConfig()


def load_state_persistence_config(main_config_yaml: dict[str, Any]) -> StatePersistenceConfig:
    """Return a validated :class:`StatePersistenceConfig` from a parsed ``main.yaml`` dict.

    Raises ``ValueError`` when the ``state_persistence:`` section is missing from
    *main_config_yaml*; downstream callers can treat the absence as a config
    error and abort the invocation.
    """
    if "state_persistence" not in main_config_yaml:
        msg = (
            "main.yaml is missing the 'state_persistence:' section; "
            "add it before invoking the state-persistence layer"
        )
        raise ValueError(msg)
    section: dict[str, Any] = main_config_yaml["state_persistence"]
    return StatePersistenceConfig.model_validate(section)
