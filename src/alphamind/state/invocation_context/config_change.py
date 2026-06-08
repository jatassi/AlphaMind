"""Activity-log emission helpers for configuration events.

``emit_distillation_config_change_entry`` (story ALP-100): wires the
distillation configuration loader into the activity-log substrate.  On
reload, the helper is called inside the open ``InvocationContext`` so the
emitted row commits atomically with the invocation row (or rolls back with
it on exception). The persisted prior-reload ``new_hash`` is consulted to
suppress a no-op append when the resolved config is byte-identical to the
prior reload — see ``state-persistence.md`` § Activity log entries.

``emit_profile_switch_entry`` (story ALP-663): wires the operator-console
profile-switch handler into the activity-log substrate.  When the outcome
is a no-op (``is_no_op=True``), the helper returns without writing so the
activity log contains no redundant entry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from alphamind.config.control_handlers.profile_switch import ProfileSwitchOutcome
from alphamind.config.models.distillation import DistillationConfig
from alphamind.portfolio_state.computations.activity_log import (
    build_distillation_config_change_entry,
    compute_distillation_config_hash,
)
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry, EventType
from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail
from alphamind.portfolio_state.events.types import EventSource
from alphamind.state.invocation_context.activity_log import (
    append_activity_log_entry,
    emit_activity_log_entry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.invocations import InvocationRow


async def emit_distillation_config_change_entry(
    handle: InvocationHandle,
    *,
    prior: DistillationConfig | None,
    new: DistillationConfig,
    timestamp: datetime,
    git_sha: str,
    entry_id: str,
    config_file: str = "config/distillation.yaml",
) -> ActivityLogEntry | None:
    """Emit a ``DISTILLATION_CONFIG_CHANGE`` entry, or suppress when no-op.

    Looks up the most recent persisted ``new_hash`` for ``config_file`` via
    ``read_most_recent_config_change_new_hash``; if it matches the current
    ``new`` config's hash, returns ``None`` without writing. Otherwise builds
    the typed entry via ``build_distillation_config_change_entry`` (which
    populates ``changes`` from the in-process ``prior`` when present and
    yields a baseline entry when ``prior`` is ``None``) and appends it to
    the open transaction via ``append_activity_log_entry``.

    Returns the entry that was persisted, or ``None`` when emission was
    suppressed.
    """
    # Lazy import to break a module-load cycle: ``state.repository.__init__``
    # eagerly imports ``activity_log_queries``, which imports this package; a
    # module-level import of ``activity_log_queries`` here closes the loop and
    # makes a cold ``import alphamind.state.repository.*`` fail. Importing inside
    # the function defers it past package initialization.
    from alphamind.state.repository.activity_log_queries import (
        read_most_recent_config_change_new_hash,
    )

    persisted_prior_hash = await read_most_recent_config_change_new_hash(
        handle.session, config_file
    )
    current_hash = compute_distillation_config_hash(new)
    if persisted_prior_hash == current_hash:
        return None
    entry = build_distillation_config_change_entry(
        prior=prior,
        new=new,
        invocation_id=handle.invocation_id,
        timestamp=timestamp,
        git_sha=git_sha,
        entry_id=entry_id,
        config_file=config_file,
    )
    if entry is None:
        return None
    append_activity_log_entry(handle, entry)
    return entry


async def emit_baseline_config_change_entry(
    *,
    handle: InvocationHandle,
    config_dir: Path,
    now: datetime,
) -> None:
    """Emit a baseline ``DISTILLATION_CONFIG_CHANGE`` entry for one invocation.

    Loads the distillation config from ``<config_dir>/distillation.yaml``
    and calls :func:`emit_distillation_config_change_entry` with
    ``prior=None``. The helper's hash-check de-dup
    (:func:`read_most_recent_config_change_new_hash`) suppresses no-op
    re-emissions on subsequent invocations with byte-identical config; on
    a fresh DB this writes one baseline entry per config-version so the
    verify script's ``check_activity_log`` succeeds even when fill collection and
    command execution emit zero entries (clean paper-DB invocation).

    The git SHA is read from the bound invocation row (stamped by
    ``insert_invocation_record`` before fill collection opened). The entry id
    follows the fill-collection / command-execution emitter convention
    (``{invocation_id}-{event_type}-{uuid4-hex}``).
    """
    # Local import keeps the state-layer module from pulling scripts into its
    # closure at import time; the loader is small and only invoked here.
    from alphamind.scripts._common import load_distillation_config

    distillation_config = load_distillation_config(config_dir / "distillation.yaml")
    row = await handle.session.get(InvocationRow, handle.invocation_id)
    if row is None:
        msg = (
            f"invocations row {handle.invocation_id!r} disappeared before "
            "baseline DISTILLATION_CONFIG_CHANGE emission; "
            "insert_invocation_record should have committed it before fill collection opened"
        )
        raise RuntimeError(msg)
    entry_id = (
        f"{handle.invocation_id}-{EventType.DISTILLATION_CONFIG_CHANGE.value}-{uuid.uuid4().hex}"
    )
    await emit_distillation_config_change_entry(
        handle,
        prior=None,
        new=distillation_config,
        timestamp=now,
        git_sha=row.git_sha_at_invocation,
        entry_id=entry_id,
    )


def emit_profile_switch_entry(
    *,
    handle: InvocationHandle,
    outcome: ProfileSwitchOutcome,
    now: datetime | None = None,
) -> None:
    """Emit a ``PROFILE_SWITCHED`` entry, or suppress when the switch is a no-op.

    When ``outcome.is_no_op`` is ``True`` (the requested profile equals the
    current one), returns without writing — no redundant entry is appended to
    the activity log.

    When ``outcome.is_no_op`` is ``False``, constructs a :class:`ProfileSwitchedDetail`
    from the outcome fields and appends one typed :class:`ActivityLogEntry` inside
    the open handle's transaction.  The row commits atomically with the surrounding
    ``InvocationContext``; an exception escaping the context rolls it back.

    ``now`` lets the caller supply the same timestamp it uses for the response
    ``applied_at`` so the activity-log row and the HTTP response carry one
    coherent wall-clock instant.  Defaults to ``datetime.now(UTC)`` when not
    supplied (F10).
    """
    if outcome.is_no_op:
        return

    detail = ProfileSwitchedDetail(
        previous_profile=outcome.previous_profile,
        new_profile=outcome.new_profile,
        is_no_op=outcome.is_no_op,
    )
    emit_activity_log_entry(
        handle,
        event_type=EventType.PROFILE_SWITCHED,
        position_id=None,
        order_id=None,
        thesis_id=None,
        timestamp=now if now is not None else datetime.now(UTC),
        detail=detail,
        source=EventSource.OPERATOR_CONSOLE,
    )
