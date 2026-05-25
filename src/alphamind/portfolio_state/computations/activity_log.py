"""Pure-function filtering and projection helpers for activity log entries."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

from alphamind.config.models.distillation import DistillationConfig
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    DistillationConfigChange,
    DistillationConfigChangeDetail,
    EventGroup,
    EventSource,
    EventType,
)

# ---------------------------------------------------------------------------
# Single-dimension filters
# ---------------------------------------------------------------------------


def filter_by_invocation_id(
    entries: tuple[ActivityLogEntry, ...],
    invocation_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose invocation_id exactly matches."""
    return tuple(e for e in entries if e.invocation_id == invocation_id)


def filter_by_event_type(
    entries: tuple[ActivityLogEntry, ...],
    event_type: EventType,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose event_type exactly matches."""
    return tuple(e for e in entries if e.event_type == event_type)


def filter_by_event_types(
    entries: tuple[ActivityLogEntry, ...],
    event_types: frozenset[EventType],
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose event_type is in event_types (union)."""
    return tuple(e for e in entries if e.event_type in event_types)


def filter_by_event_group(
    entries: tuple[ActivityLogEntry, ...],
    event_group: EventGroup,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose event_group exactly matches."""
    return tuple(e for e in entries if e.event_group == event_group)


def filter_by_source(
    entries: tuple[ActivityLogEntry, ...],
    source: EventSource,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose source exactly matches."""
    return tuple(e for e in entries if e.source == source)


def filter_by_position_id(
    entries: tuple[ActivityLogEntry, ...],
    position_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose position_id matches; entries with position_id None are excluded."""
    return tuple(e for e in entries if e.position_id == position_id)


def filter_by_order_id(
    entries: tuple[ActivityLogEntry, ...],
    order_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose order_id exactly matches; entries with order_id None are excluded."""
    return tuple(e for e in entries if e.order_id == order_id)


def filter_by_thesis_id(
    entries: tuple[ActivityLogEntry, ...],
    thesis_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries whose thesis_id exactly matches; entries with thesis_id None are excluded."""
    return tuple(e for e in entries if e.thesis_id == thesis_id)


def filter_by_timestamp_range(
    entries: tuple[ActivityLogEntry, ...],
    *,
    start: datetime | None,
    end: datetime | None,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries in the half-open interval [start, end).

    Both start and end must be tz-aware UTC or None.  Raises ValueError on
    mixed tz-aware / tz-naive inputs.  A reversed range (start > end) returns
    an empty tuple.
    """
    if (
        start is not None
        and end is not None
        and (start.tzinfo is not None) != (end.tzinfo is not None)
    ):
        msg = "start and end must both be tz-aware or both be tz-naive"
        raise ValueError(msg)

    return tuple(
        e
        for e in entries
        if (start is None or e.timestamp >= start) and (end is None or e.timestamp < end)
    )


# ---------------------------------------------------------------------------
# Category-5 projection helpers
# ---------------------------------------------------------------------------


def intra_invocation_changelog(
    entries: tuple[ActivityLogEntry, ...],
    invocation_id: str,
) -> tuple[ActivityLogEntry, ...]:
    """Return entries scoped to a single invocation (category-5a)."""
    return filter_by_invocation_id(entries, invocation_id)


def pm_decision_log(
    entries: tuple[ActivityLogEntry, ...],
) -> tuple[ActivityLogEntry, ...]:
    """Return entries with event_type PM_DECISION (category-5b)."""
    return filter_by_event_type(entries, EventType.PM_DECISION)


def position_modification_trail(
    entries: tuple[ActivityLogEntry, ...],
    position_ids: tuple[str, ...],
) -> dict[str, tuple[ActivityLogEntry, ...]]:
    """Return per-position entry tuples for each requested position_id (category-5c).

    Position IDs with no matching entries are absent from the result dict.
    Empty position_ids returns {}.
    """
    if not position_ids:
        return {}
    result: dict[str, tuple[ActivityLogEntry, ...]] = {}
    for pid in position_ids:
        matching = filter_by_position_id(entries, pid)
        if matching:
            result[pid] = matching
    return result


def recent_pm_decisions_for_position(
    entries: tuple[ActivityLogEntry, ...],
    position_id: str,
    limit: int,
) -> tuple[ActivityLogEntry, ...]:
    """Return the last ``limit`` PM_DECISION entries for a specific position.

    Assumes chronological input order.  Raises ValueError for limit <= 0.
    """
    if limit <= 0:
        msg = "limit must be a positive integer"
        raise ValueError(msg)
    matching = filter_by_position_id(pm_decision_log(entries), position_id)
    return matching[-limit:]


# ---------------------------------------------------------------------------
# Composition helpers
# ---------------------------------------------------------------------------


def partition_by_event_group(
    entries: tuple[ActivityLogEntry, ...],
) -> dict[EventGroup, tuple[ActivityLogEntry, ...]]:
    """Group entries by event_group in a single pass.

    Groups with no entries are absent from the result.
    """
    groups: defaultdict[EventGroup, list[ActivityLogEntry]] = defaultdict(list)
    for e in entries:
        groups[e.event_group].append(e)
    return {g: tuple(es) for g, es in groups.items()}


def chronological_sort(
    entries: tuple[ActivityLogEntry, ...],
) -> tuple[ActivityLogEntry, ...]:
    """Return entries sorted by timestamp ascending; ties broken by entry_id ascending."""
    return tuple(sorted(entries, key=lambda e: (e.timestamp, e.entry_id)))


# ---------------------------------------------------------------------------
# Distillation config-change helpers (story 14a contract)
# ---------------------------------------------------------------------------


def compute_distillation_config_hash(config: DistillationConfig) -> str:
    """Return the 64-hex-char SHA-256 of the canonical-JSON form of ``config``.

    Mirrors the canonical-JSON discipline in
    ``alphamind.config.snapshot.compute_snapshot_hash``: keys are
    lexicographically sorted at every level, enum values are emitted as their
    string ``value``. Two ``DistillationConfig`` instances with byte-identical
    field values produce byte-identical digests across runs and machines.
    """
    payload = config.model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _walk_diff_leaves(prior: Any, new: Any, prefix: str) -> list[DistillationConfigChange]:
    """Walk two parallel JSON-shaped values and emit changes for differing leaves."""
    if isinstance(prior, dict) and isinstance(new, dict):
        # The two configs share the same Pydantic schema, so their dumps share keys.
        changes: list[DistillationConfigChange] = []
        for key in prior.keys() | new.keys():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            changes.extend(_walk_diff_leaves(prior.get(key), new.get(key), child_prefix))
        return changes
    if prior == new:
        return []
    return [DistillationConfigChange(key_path=prefix, old_value=prior, new_value=new)]


def compute_distillation_config_diff(
    prior: DistillationConfig | None, new: DistillationConfig
) -> tuple[DistillationConfigChange, ...]:
    """Return per-leaf changes between ``prior`` and ``new``, sorted by ``key_path``.

    When ``prior is None`` the function returns ``()`` — the first-reload
    baseline carries no per-key changes; the ``prior_hash=None`` field is what
    distinguishes the bootstrap entry from a subsequent reload.

    The walk uses ``model_dump(mode="json")`` on both configs so enum values
    serialise as their string ``value`` and floats/ints/bools become JSON
    scalars. The two configs must share the same Pydantic schema; the helper
    does not handle structural drift across schema versions — that is a
    separate migration concern.
    """
    if prior is None:
        return ()
    prior_dump = prior.model_dump(mode="json")
    new_dump = new.model_dump(mode="json")
    changes = _walk_diff_leaves(prior_dump, new_dump, "")
    return tuple(sorted(changes, key=lambda c: c.key_path))


def build_distillation_config_change_entry(
    *,
    prior: DistillationConfig | None,
    new: DistillationConfig,
    invocation_id: str,
    timestamp: datetime,
    git_sha: str,
    entry_id: str,
    config_file: str = "config/distillation.yaml",
) -> ActivityLogEntry | None:
    """Compose a ``DISTILLATION_CONFIG_CHANGE`` activity-log entry.

    Returns ``None`` when ``prior is not None`` and the new and prior configs
    hash identically — the no-change suppression rule. The first-ever reload
    (``prior is None``) returns a baseline entry with ``prior_hash=None`` and
    an empty ``changes`` tuple regardless of the diff result.

    The function is pure: it performs no DB writes, no clock reads, and no
    git lookups. The caller supplies ``timestamp``, ``git_sha``, ``entry_id``,
    and ``invocation_id``; the emission story (`14a-config-change-emission`)
    wires those at the integration site.
    """
    new_hash = compute_distillation_config_hash(new)
    prior_hash = compute_distillation_config_hash(prior) if prior is not None else None

    if prior is not None and prior_hash == new_hash:
        return None

    detail = DistillationConfigChangeDetail(
        config_file=config_file,
        prior_hash=prior_hash,
        new_hash=new_hash,
        changes=compute_distillation_config_diff(prior, new),
        git_sha=git_sha,
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=EventType.DISTILLATION_CONFIG_CHANGE,
        event_group=EventGroup.CONFIGURATION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.CONFIG_RELOAD,
        detail=detail,
    )
