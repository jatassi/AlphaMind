"""``DISTILLATION_CONFIG_CHANGE`` emission helper (story ALP-100).

Wires the distillation configuration loader into the activity-log substrate.
On reload, the helper is called inside the open ``InvocationContext`` so the
emitted row commits atomically with the invocation row (or rolls back with
it on exception). The persisted prior-reload ``new_hash`` is consulted to
suppress a no-op append when the resolved config is byte-identical to the
prior reload — see ``state-persistence.md`` § Activity log entries.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.config.models.distillation import DistillationConfig
from alphamind.execution.state_persistence.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.repository.activity_log_queries import (
    read_most_recent_config_change_new_hash,
)
from alphamind.portfolio_state.computations.activity_log import (
    build_distillation_config_change_entry,
    compute_distillation_config_hash,
)
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry


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
