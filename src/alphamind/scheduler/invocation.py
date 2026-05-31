"""Per-invocation row composition.

Builds the 22-field ``InvocationRecord`` every pipeline invocation writes
and inserts it via :func:`insert_invocation_row` in its own short
transaction so the row is durable + visible to fresh-session reads before
Phase 1 opens.

The orchestrator entry-point :func:`insert_invocation_record` composes the
record (minting the invocation_id, loading + persisting the pipeline-config
snapshot, computing the data-source freshness JSON) and returns the
``(invocation_id, pipeline_config)`` pair the orchestrator threads through
the per-phase transactions.

See ``docs/design/05-execution-layer/state-persistence.md`` § Invocation
records for the column contract and parent issue ``ALP-431`` § Pre-resolved
configuration decisions (J) for the bootstrap path on first-ever invocation.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.archive_layout import CALIBRATION_SNAPSHOT_FILENAME, invocation_archive_dir
from alphamind._kernel.atomic_io import atomic_write_text
from alphamind._kernel.mode import PipelineMode
from alphamind.config.load import PipelineConfig, load_full_config
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import RuntimeDimensions
from alphamind.persistence.models import CollectionRuns
from alphamind.state.invocation_context.context import (
    insert_invocation_row,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    TriggerType,
)
from alphamind.state.invocation_id import mint_invocation_id


async def build_invocation_record(  # noqa: PLR0913 — signature pinned by story 03a spec
    *,
    session: AsyncSession,
    process_lifetime_id: str,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    runtime: RuntimeDimensions,
    pipeline_config: PipelineConfig,
    archive_root: Path,
    invocation_id: str,
    now: datetime,
) -> InvocationRecord:
    """Compose all 22 ``InvocationRecord`` fields from the supplied inputs.

    ``firing_run_type`` is also carried on ``runtime.firing_trigger``; the two
    must agree because they describe the same identity dimension. We assert the
    invariant here so a story-03b orchestrator bug that diverges them fails
    fast at record build rather than letting a misattributed row reach the DB.
    """
    if firing_run_type is not runtime.firing_trigger:
        msg = (
            f"firing_run_type={firing_run_type!r} disagrees with "
            f"runtime.firing_trigger={runtime.firing_trigger!r}"
        )
        raise ValueError(msg)

    start_at = _isoformat_z(now)
    git_sha = _git_rev_parse_head()
    calibration_path = _persist_data_calibration_snapshot(
        archive_root=archive_root, invocation_id=invocation_id, as_of=now
    )
    data_source_freshness_json = await _compute_data_source_freshness_json(session)

    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=process_lifetime_id,
        start_at=start_at,
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type=trigger_type,
        trigger_source=trigger_source,
        trigger_reason=trigger_reason,
        git_sha_at_invocation=git_sha,
        active_profile=pipeline_config.resolved.profile_label,
        active_regime=runtime.active_regime.value,
        active_mode=PipelineMode.from_config_mode(runtime.active_mode).to_active_mode_literal(),
        active_overlays_json=json.dumps([o.value for o in runtime.active_overlays]),
        resolved_config_hash=pipeline_config.snapshot.hash,
        resolved_config_snapshot_path=str(pipeline_config.snapshot.path),
        feature_flags_snapshot_json=json.dumps(
            pipeline_config.snapshot.feature_flags_snapshot, sort_keys=True
        ),
        data_calibration_state_snapshot_path=str(calibration_path),
        data_source_freshness_json=data_source_freshness_json,
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _isoformat_z(now: datetime) -> str:
    """Format ``now`` as ISO 8601 with a literal ``Z`` UTC suffix."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_INVOCATION_ID_PATTERN = re.compile(r"^inv-\d{8}T\d{6}Z-[0-9a-f]{8}$")


def _persist_data_calibration_snapshot(
    *,
    archive_root: Path,
    invocation_id: str,
    as_of: datetime,
) -> Path:
    """Copy the most recent prior invocation's calibration state into this invocation's dir.

    Implements the bootstrap path described in parent issue ``ALP-431`` § Pre-resolved
    configuration decisions (J). Scans ``<archive_root>/*/`` for the
    lexicographically-greatest per-invocation directory (excluding ``invocation_id``)
    whose ``data_calibration_state.json`` exists, and atomically copies that content
    into ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/data_calibration_state.json``
    (date-partitioned canonical layout per ALP-689 followup).
    When no prior snapshot is found (first-ever invocation) the target file is
    initialized to ``{}``.

    The :mod:`alphamind.distillation.calibration_snapshot` module does not currently
    expose a "latest snapshot path" helper, so the directory scan lives inline here
    per story 03a's spec.
    """
    target_path = (
        invocation_archive_dir(archive_root=archive_root, as_of=as_of, invocation_id=invocation_id)
        / CALIBRATION_SNAPSHOT_FILENAME
    )

    prior_content = _find_latest_prior_calibration_content(
        archive_root=archive_root, current_invocation_id=invocation_id
    )
    payload = prior_content if prior_content is not None else "{}"

    atomic_write_text(target_path, payload)
    return target_path


def _find_latest_prior_calibration_content(
    *,
    archive_root: Path,
    current_invocation_id: str,
) -> str | None:
    """Return the content of the latest prior calibration snapshot, or ``None``.

    Scans the date-partitioned archive layout (``<archive_root>/*/<invocation_id>/``)
    for any invocation directory that is not ``current_invocation_id``, sorts
    by invocation-id name (lexicographic order equals chronological order for
    the ``inv-YYYYMMDDTHHMMSSZ-<hex>`` format), and returns the content of
    the most recent existing ``data_calibration_state.json``.
    """
    if not archive_root.exists():
        return None

    # Collect all per-invocation directories across date partitions, filtering
    # to those whose name matches the invocation-id pattern and is not the
    # current invocation being seeded.
    candidates = sorted(
        (
            entry
            for date_dir in archive_root.iterdir()
            if date_dir.is_dir()
            for entry in date_dir.iterdir()
            if entry.is_dir()
            and entry.name != current_invocation_id
            and _INVOCATION_ID_PATTERN.match(entry.name) is not None
        ),
        key=lambda entry: entry.name,
        reverse=True,
    )
    for candidate in candidates:
        snapshot_path = candidate / CALIBRATION_SNAPSHOT_FILENAME
        if snapshot_path.exists():
            return snapshot_path.read_text(encoding="utf-8")
    return None


async def _compute_data_source_freshness_json(session: AsyncSession) -> str:
    """Build the freshness JSON map ``{provider: latest_pull_isoformat or null}``.

    The provider list is derived from ``config/data_sources.yaml``'s top-level
    ``providers`` keys. The per-provider value is the ``completed_at`` of the
    most recent ``collection_runs`` row whose ``status='success'`` and whose
    ``collector`` prefix (before the first ``.``) matches the provider name.
    Providers with no successful pull map to ``null``.
    """
    providers = _data_source_provider_names()
    freshness: dict[str, str | None] = dict.fromkeys(providers, None)

    stmt = select(CollectionRuns.collector, CollectionRuns.completed_at).where(
        CollectionRuns.status == "success",
        CollectionRuns.completed_at.is_not(None),
    )
    result = await session.execute(stmt)
    for collector, completed_at in result.all():
        provider, _, _ = collector.partition(".")
        if provider not in freshness:
            continue
        current = freshness[provider]
        if current is None or completed_at > current:
            freshness[provider] = completed_at

    return json.dumps(freshness, sort_keys=True)


@cache
def _data_source_provider_names() -> tuple[str, ...]:
    """Return the alphabetized tuple of provider names defined in ``data_sources.yaml``.

    Cached so repeated invocations do not re-read the YAML file. The provider
    set is process-stable: a config edit requires a process restart per the
    pre-resolved-configuration decisions in ``ALP-431``.
    """
    import yaml  # local import keeps module-load cost lean

    config_path = Path(__file__).resolve().parents[3] / "config" / "data_sources.yaml"
    with config_path.open(encoding="utf-8") as fh:
        payload: dict[str, object] = yaml.safe_load(fh) or {}
    providers = payload.get("providers", {})
    if not isinstance(providers, dict):
        msg = f"data_sources.yaml providers must be a mapping, got {type(providers)!r}"
        raise TypeError(msg)
    return tuple(sorted(providers))


def _git_rev_parse_head() -> str:
    """Return the 40-character SHA of ``HEAD`` via ``git rev-parse``.

    A non-zero exit raises :class:`subprocess.CalledProcessError`; the spec
    requires the error to propagate to the caller so the invocation can be
    aborted before the row is composed.
    """
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


# Backwards-compatible alias for the prior private name. New call sites
# should import :func:`alphamind.state.invocation_id.mint_invocation_id`
# directly — see ALP-715 review F5 / the "Composition roots sit above the
# domain" contract in ``.importlinter`` (``execution.*`` cannot reach into
# ``scheduler.*``, so the canonical helper lives one layer down).
_mint_invocation_id = mint_invocation_id


async def insert_invocation_record(  # noqa: PLR0913 — composition surface threads typed inputs
    *,
    session_factory: async_sessionmaker[AsyncSession],
    process_lifetime_id: str,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    runtime: RuntimeDimensions,
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    now: datetime,
) -> tuple[str, PipelineConfig]:
    """Compose the invocation record, persist provenance, and insert the row.

    Steps in order:

    1. Mint ``invocation_id`` via :func:`_mint_invocation_id`.
    2. Open a short read-only session for the freshness query.
    3. Call :func:`load_full_config` to validate, compose, and persist the
       resolved configuration snapshot under
       ``<archive_root>/<YYYY-MM-DD>/<invocation_id>/resolved_config.json``
       (date-partitioned canonical layout per ALP-689 followup).
    4. Build the ``InvocationRecord`` via :func:`build_invocation_record` —
       this step persists the data-calibration snapshot inline and computes
       the freshness JSON against the short session.
    5. Call :func:`insert_invocation_row` to insert + commit the row in its
       own short-lived transaction.

    Returns ``(invocation_id, pipeline_config)``. The orchestrator threads
    both through the per-phase transactions: the id identifies the row that
    Phase 1 / Phase 2 stamp; the config is the resolved snapshot the row's
    ``resolved_config_snapshot_path`` points at, reused without re-loading.
    """
    invocation_id = _mint_invocation_id(now)

    async with session_factory() as short_session:
        pipeline_config = load_full_config(
            config_dir=config_dir,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id=invocation_id,
            runtime=runtime,
            today=now.astimezone(UTC).date(),
            as_of=now,
        )
        record = await build_invocation_record(
            session=short_session,
            process_lifetime_id=process_lifetime_id,
            trigger_type=trigger_type,
            trigger_source=trigger_source,
            trigger_reason=trigger_reason,
            firing_run_type=firing_run_type,
            runtime=runtime,
            pipeline_config=pipeline_config,
            archive_root=archive_root,
            invocation_id=invocation_id,
            now=now,
        )

    await insert_invocation_row(session_factory, record)
    return invocation_id, pipeline_config
