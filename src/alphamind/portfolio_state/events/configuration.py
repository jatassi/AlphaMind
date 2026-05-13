"""Configuration event details — distillation-config reloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alphamind.portfolio_state.events.types import EventGroup, EventType


@dataclass(frozen=True, slots=True)
class DistillationConfigChange:
    """One per-key change inside a ``DistillationConfigChangeDetail.changes`` tuple.

    ``key_path`` is the dotted path matching the ``DistillationConfig`` field
    structure (for example ``anomaly_detection.volume_anomaly_sigma``).
    ``old_value`` and ``new_value`` carry whatever the Pydantic JSON dump
    produced — scalars, ints, floats, bools, or string-valued enums.
    """

    key_path: str
    old_value: Any
    new_value: Any

    def __post_init__(self) -> None:
        if not isinstance(self.key_path, str):
            msg = f"key_path must be a string; got {type(self.key_path).__name__}"
            raise TypeError(msg)
        if not self.key_path:
            msg = "key_path must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DistillationConfigChangeDetail:
    """Detail payload for ``DISTILLATION_CONFIG_CHANGE`` events.

    Emitted by the configuration loader at the start of an invocation when the
    reloaded ``DistillationConfig`` differs from the prior reload, or when no
    prior reload exists. Identical reloads produce no entry — see
    ``build_distillation_config_change_entry`` in
    ``alphamind.portfolio_state.computations.activity_log``.
    """

    prior_hash: str | None
    new_hash: str
    changes: tuple[DistillationConfigChange, ...]
    git_sha: str
    config_file: str = field(default="config/distillation.yaml")

    def __post_init__(self) -> None:
        if not self.config_file:
            msg = "config_file must be non-empty"
            raise ValueError(msg)
        if not self.new_hash:
            msg = "new_hash must be non-empty"
            raise ValueError(msg)
        if not self.git_sha:
            msg = "git_sha must be non-empty"
            raise ValueError(msg)
        for prev, curr in zip(self.changes, self.changes[1:], strict=False):
            if prev.key_path >= curr.key_path:
                msg = (
                    "changes must be sorted by key_path ascending "
                    f"(got {prev.key_path!r} then {curr.key_path!r})"
                )
                raise ValueError(msg)


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (
        EventType.DISTILLATION_CONFIG_CHANGE,
        DistillationConfigChangeDetail,
        EventGroup.CONFIGURATION,
    ),
]


__all__ = [
    "DistillationConfigChange",
    "DistillationConfigChangeDetail",
]
