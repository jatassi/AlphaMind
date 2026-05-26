"""End-to-end configuration loader entry point (story 08).

Single function the pipeline calls at invocation start to:

1. Read every YAML file via per-file Pydantic validation (parse-time layer).
2. Resolve cross-references — agent tools, env vars, regime/profile rule
   coverage — via :func:`validate_cross_references` (cross-reference layer).
3. Compose the runtime-resolved snapshot via :func:`compose_config`.
4. Run the semantic-self-test invariants over the full composition matrix via
   :func:`validate_semantic_invariants` (semantic layer).
5. Persist the resolved snapshot to the per-invocation provenance directory
   via :func:`persist_snapshot`.
6. Return both the resolved config and snapshot metadata wrapped in a
   :class:`PipelineConfig`.

Exceptions raised by any layer propagate unchanged: parse-time failures bubble
up as ``pydantic.ValidationError``; cross-reference failures as
``CrossReferenceError``; semantic failures as ``SemanticInvariantError``.
``configuration-management.md`` § Validation says "A failure at any layer
aborts the invocation and alerts the operator" — the loader's caller (the
pipeline scheduler) is the layer that catches and routes those errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from alphamind.config.loaders import (
    load_modes,
    load_overlays,
    load_profiles,
    load_regimes,
    load_run_types,
    read_env_keys,
    read_yaml_file,
)
from alphamind.config.models.agents import AgentsConfig
from alphamind.config.models.assets import AssetsConfig
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.digest import DigestConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.guardrails import GuardrailsConfig
from alphamind.config.models.llm_failure import LLMFailureConfig
from alphamind.config.models.main import MainConfig
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.config.resolver import (
    LoadedConfig,
    ResolvedConfig,
    RuntimeDimensions,
    compose_config,
)
from alphamind.config.snapshot import SnapshotResult, persist_snapshot
from alphamind.config.tools import REGISTERED_TOOLS
from alphamind.config.validation.cross_reference import validate_cross_references
from alphamind.config.validation.semantic import (
    enumerate_compositions,
    validate_semantic_invariants,
)


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Pipeline-facing wrapper bundling the resolved snapshot and persistence metadata.

    Named ``PipelineConfig`` to avoid a collision with story 05's
    :class:`alphamind.config.resolver.LoadedConfig`, which carries the parsed
    Pydantic-model bundle handed to :func:`compose_config`. Both are
    invocation-scoped data carriers; this one is what the pipeline runs against.

    ``loaded`` retains the parsed input bundle so downstream consumers (e.g.
    the regime-adaptation resolver wiring) can read it without re-running the
    14-file YAML parse pass.
    """

    resolved: ResolvedConfig
    snapshot: SnapshotResult
    loaded: LoadedConfig


def parse_loaded_config(config_dir: Path) -> LoadedConfig:
    """Parse every YAML in ``config_dir`` into the resolver's input bundle.

    Each per-file ``model_validate`` invocation runs the parse-time validators
    (story 03* / 04*); ``pydantic.ValidationError`` propagates on the first
    failure. The bundle loaders already encapsulate their subdirectory layout.

    Public so the regime-adaptation resolver wiring (ALP-513) can rebuild
    :class:`LoadedConfig` for its input fan without re-running the full
    pipeline-config snapshot pass.
    """
    return LoadedConfig(
        main=MainConfig.model_validate(read_yaml_file(config_dir / "main.yaml")),
        scheduler=SchedulerConfig.model_validate(read_yaml_file(config_dir / "scheduler.yaml")),
        venue=VenueConfig.model_validate(read_yaml_file(config_dir / "venue.yaml")),
        execution=ExecutionConfig.model_validate(read_yaml_file(config_dir / "execution.yaml")),
        guardrails=GuardrailsConfig.model_validate(read_yaml_file(config_dir / "guardrails.yaml")),
        llm_failure=LLMFailureConfig.model_validate(
            read_yaml_file(config_dir / "llm_failure.yaml")
        ),
        digest=DigestConfig.model_validate(read_yaml_file(config_dir / "digest.yaml")),
        assets=AssetsConfig.model_validate(read_yaml_file(config_dir / "assets.yaml")),
        agents=AgentsConfig.model_validate(read_yaml_file(config_dir / "agents.yaml")),
        continuous_monitor=ContinuousMonitorConfig.model_validate(
            read_yaml_file(config_dir / "continuous_monitor.yaml")
        ),
        profiles=load_profiles(config_dir),
        regimes=load_regimes(config_dir),
        modes=load_modes(config_dir),
        overlays=load_overlays(config_dir),
        run_types=load_run_types(config_dir),
    )


def load_full_config(
    *,
    config_dir: Path,
    env_path: Path,
    archive_root: Path,
    invocation_id: str,
    runtime: RuntimeDimensions,
    today: date,
    as_of: datetime,
) -> PipelineConfig:
    """Load, validate, resolve, persist, and return one invocation's config.

    Pure-orchestration: every dependency (paths, runtime values) is a parameter,
    every step is a documented layer in ``configuration-management.md``
    § Validation, and no exception is caught. The function does I/O — reading
    YAML, reading ``.env``, writing the snapshot — but no other side effects.

    Parameters group as ``RuntimeDimensions`` (regime, mode, overlays, firing
    trigger) to keep the call surface aligned with story 05's
    :func:`compose_config` and stay under the 8-arg cap.

    ``as_of`` is the invocation timestamp used to place the resolved-config
    snapshot under the date-partitioned canonical archive layout (ALP-689
    followup).
    """
    # 1. Parse-time validation. ValidationError propagates.
    inputs = parse_loaded_config(config_dir)

    # 2. Cross-reference validation. CrossReferenceError propagates.
    env_keys = read_env_keys(env_path)
    validate_cross_references(
        inputs,
        env_keys=env_keys,
        registered_tools=REGISTERED_TOOLS,
    )

    # 3. Composition.
    resolved = compose_config(inputs, runtime)

    # 4. Semantic-self-test validation. SemanticInvariantError propagates.
    composed_matrix = enumerate_compositions(inputs)
    validate_semantic_invariants(
        guardrails=inputs.guardrails,
        assets=inputs.assets,
        digest=inputs.digest,
        profiles=inputs.profiles,
        regimes=inputs.regimes,
        composed_configs=composed_matrix,
        today=today,
    )

    # 5. Snapshot persistence.
    snapshot_result = persist_snapshot(
        resolved, archive_root=archive_root, invocation_id=invocation_id, as_of=as_of
    )

    # 6. Wrap.
    return PipelineConfig(resolved=resolved, snapshot=snapshot_result, loaded=inputs)
