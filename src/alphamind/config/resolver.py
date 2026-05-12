"""Composition resolver for the configuration tree (story 05).

Pure function that folds the runtime-resolved identity dimensions (active
regime, active mode, active overlays, firing trigger) into the operator-pinned
profile to produce a single ``ResolvedConfig`` snapshot. The cascade order is
fixed per ``docs/design/configuration-management.md`` § Composition model:

1. Profile base (rule_values from ``profiles[main.active_profile]``)
2. Regime multiplier (multiplicative across every rule in the profile)
3. Overlay multipliers (multiplicative, in operator-supplied order; partial)
4. Mode behavioral transform (per-agent restrictions)
5. Run-type filter and clamp (agent roster + overrides + news-digest depth)
6. Feature-flag closure (drop-not-zero per profile flags)

The resolver assumes its inputs have already passed parse-time, cross-reference
(story 06a), and semantic-self-test (story 06b) validation. It performs no I/O
or logging — equal inputs produce equal outputs by construction so the snapshot
hash recorded in the invocation record (story 07) is deterministic.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from alphamind.config.models.agents import AgentName, AgentsConfig
from alphamind.config.models.assets import AssetsConfig
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.digest import DigestConfig
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.guardrails import GuardrailsConfig
from alphamind.config.models.llm_failure import LLMFailureConfig
from alphamind.config.models.main import (
    ExecutionMode,
    MainConfig,
    Paths,
    Profile,
)
from alphamind.config.models.modes import (
    AnalystOutputMode,
    CommandType,
    Mode,
    ModeConfig,
    PendingOrdersDefault,
    PmEmphasis,
    StrategistAction,
    StrategistOutputMode,
)
from alphamind.config.models.overlays import (
    Overlay,
    PreEventOverlay,
    StressOverlay,
)
from alphamind.config.models.profiles import (
    FeatureFlags,
    ProfileConfig,
    TokenBudgetRange,
)
from alphamind.config.models.regimes import Regime, RegimeConfig
from alphamind.config.models.run_types import RunType, RunTypeConfig
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import VenueConfig

# Rule-key prefixes governed by feature flags. When a flag is false, every
# rule whose key starts with one of these prefixes is dropped from
# ``ResolvedConfig.rule_values`` per
# ``docs/design/configuration-management.md`` § Feature flag semantics.
_OPTIONS_RULE_PREFIXES: tuple[str, ...] = ("options_", "portfolio_theta_", "portfolio_vega_")
_OPTIONS_RULE_KEYS: frozenset[str] = frozenset({"position_max_loss_options_pct"})

_SHORT_RULE_PREFIXES: tuple[str, ...] = ("net_short_", "total_short_", "single_short_")
_SHORT_RULE_KEYS: frozenset[str] = frozenset({"borrow_cost_budget_pct_per_day"})


def _is_options_rule(rule_id: str) -> bool:
    return rule_id in _OPTIONS_RULE_KEYS or rule_id.startswith(_OPTIONS_RULE_PREFIXES)


def _is_short_rule(rule_id: str) -> bool:
    return rule_id in _SHORT_RULE_KEYS or rule_id.startswith(_SHORT_RULE_PREFIXES)


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """Hashable snapshot consumed by agents and the engine for one invocation.

    Fields fall into three groups:

    * **Composition outputs.** ``rule_values`` and the per-agent maps are the
      fold of profile, regime, overlays, mode, and run-type. They are the
      reason the resolver exists.
    * **Pass-through Pydantic models.** Already frozen via their
      ``model_config`` — re-exported on the dataclass for one-stop callsite
      ergonomics.
    * **Pass-through scalars.** ``execution_mode`` and ``paths`` from
      ``main.yaml``; ``news_digest_*`` lifted from the active run-type.
    """

    profile: ProfileConfig
    profile_label: str
    regime: RegimeConfig
    regime_label: str
    mode: ModeConfig
    active_overlays: tuple[PreEventOverlay | StressOverlay, ...]
    run_type: RunTypeConfig
    feature_flags: FeatureFlags
    rule_values: Mapping[str, float]
    agent_token_budgets: Mapping[AgentName, TokenBudgetRange]
    enabled_agents: tuple[AgentName, ...]
    agent_overrides: Mapping[AgentName, Mapping[str, Any]]
    analyst_output_mode: AnalystOutputMode
    strategist_output_mode: StrategistOutputMode
    strategist_allowed_actions: tuple[StrategistAction, ...]
    pending_orders_default: PendingOrdersDefault
    pm_allowed_command_types: tuple[CommandType, ...]
    pm_emphasis: PmEmphasis
    news_digest_top_n_per_sector: int
    news_digest_top_n_high_priority: int
    paths: Paths
    execution_mode: ExecutionMode
    scheduler: SchedulerConfig
    venue: VenueConfig
    execution: ExecutionConfig
    guardrails: GuardrailsConfig
    llm_failure: LLMFailureConfig
    digest: DigestConfig
    assets: AssetsConfig
    agents: AgentsConfig
    continuous_monitor: ContinuousMonitorConfig

    def __hash__(self) -> int:
        # Pydantic frozen models with list/dict fields are not hashable, and
        # the composed mappings rule_values/agent_token_budgets/agent_overrides
        # are read-only views over dicts (also not hashable). Hashing routes
        # Pydantic models through deterministic ``model_dump_json`` and the
        # composed mappings through sorted item-tuples — Pydantic 2 serializes
        # fields in declaration order, so equal instances produce equal JSON
        # strings without further configuration.
        return hash(
            (
                self.profile.model_dump_json(),
                self.profile_label,
                self.regime.model_dump_json(),
                self.regime_label,
                self.mode.model_dump_json(),
                tuple(o.model_dump_json() for o in self.active_overlays),
                self.run_type.model_dump_json(),
                self.feature_flags.model_dump_json(),
                tuple(sorted(self.rule_values.items())),
                tuple(sorted(self.agent_token_budgets.items(), key=_by_agent_name)),
                self.enabled_agents,
                tuple(
                    (agent, tuple(sorted(override.items())))
                    for agent, override in sorted(self.agent_overrides.items(), key=_by_agent_name)
                ),
                self.analyst_output_mode,
                self.strategist_output_mode,
                self.strategist_allowed_actions,
                self.pending_orders_default,
                self.pm_allowed_command_types,
                self.pm_emphasis,
                self.news_digest_top_n_per_sector,
                self.news_digest_top_n_high_priority,
                self.paths.model_dump_json(),
                self.execution_mode,
                self.scheduler.model_dump_json(),
                self.venue.model_dump_json(),
                self.execution.model_dump_json(),
                self.guardrails.model_dump_json(),
                self.llm_failure.model_dump_json(),
                self.digest.model_dump_json(),
                self.assets.model_dump_json(),
                self.agents.model_dump_json(),
                self.continuous_monitor.model_dump_json(),
            )
        )


def _by_agent_name(item: tuple[AgentName, Any]) -> str:
    """Sort key for ``AgentName``-keyed mappings: the enum's string value."""
    return item[0].value


def _apply_regime(
    rule_values: dict[str, float], regime_multipliers: Mapping[str, float]
) -> dict[str, float]:
    """Multiply each rule by the matching regime multiplier.

    Raises ``KeyError`` naming the rule if the regime omits a multiplier the
    profile carries — a structural error post-validation.
    """
    composed: dict[str, float] = {}
    for rule_id, base_value in rule_values.items():
        if rule_id not in regime_multipliers:
            raise KeyError(
                f"Regime multiplier map is missing rule {rule_id!r} present in the "
                f"profile's rule_values; this is a structural error that should be "
                f"caught by cross-reference validation (story 06a)"
            )
        composed[rule_id] = base_value * regime_multipliers[rule_id]
    return composed


def _apply_overlays(
    rule_values: dict[str, float],
    active_overlays: tuple[Overlay, ...],
    overlay_bundle: Mapping[Overlay, PreEventOverlay | StressOverlay],
) -> dict[str, float]:
    """Multiply each rule by every active overlay's matching multiplier.

    Overlay maps are partial — a missing rule on an overlay is unaffected.
    ``active_overlays`` order is preserved for review-surface stability;
    arithmetic is commutative.
    """
    composed = dict(rule_values)
    for overlay in active_overlays:
        overlay_config = overlay_bundle[overlay]
        for rule_id, multiplier in overlay_config.multipliers.items():
            if rule_id in composed:
                composed[rule_id] = composed[rule_id] * multiplier
    return composed


def _close_feature_flags(rule_values: Mapping[str, float], flags: FeatureFlags) -> dict[str, float]:
    """Drop rules governed by disabled feature flags.

    Closure is **drop**, not zero-out — see story 05 Notes.
    """
    closed: dict[str, float] = {}
    for rule_id, value in rule_values.items():
        if not flags.options_enabled and _is_options_rule(rule_id):
            continue
        if not flags.short_selling_enabled and _is_short_rule(rule_id):
            continue
        closed[rule_id] = value
    return closed


def _frozen_mapping(items: Mapping[Any, Any]) -> Mapping[Any, Any]:
    """Return a read-only view over a copy of ``items``."""
    return MappingProxyType(dict(items))


def _frozen_overrides(
    overrides: Mapping[AgentName, Mapping[str, Any]],
) -> Mapping[AgentName, Mapping[str, Any]]:
    """Per-agent overrides need a read-only outer view *and* read-only inner views."""
    return MappingProxyType(
        {agent: MappingProxyType(dict(override)) for agent, override in overrides.items()}
    )


def _agent_token_budgets_from_profile(
    profile: ProfileConfig,
) -> dict[AgentName, TokenBudgetRange]:
    """Project the profile's string-keyed token budgets onto ``AgentName`` members.

    The profile carries free-string keys; cross-reference validation (story
    06a) is responsible for ensuring each key is a valid ``AgentName`` member.
    Unknown keys are silently dropped so the resolver remains pure when the
    upstream validator has run; if a downstream caller cares about completeness
    they consume the validator's output, not the resolver's.
    """
    members = {member.value: member for member in AgentName}
    return {
        members[key]: budget
        for key, budget in profile.agent_token_budgets.items()
        if key in members
    }


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """Operator-loaded configuration objects passed to the composition resolver.

    Groups the fourteen parsed Pydantic models the upstream loader produces.
    Bundling them keeps ``compose_config`` 's call-site fan-out manageable
    without changing the resolver's input contract: each named field below is
    one of the inputs the story-05 spec lists.
    """

    main: MainConfig
    scheduler: SchedulerConfig
    venue: VenueConfig
    execution: ExecutionConfig
    guardrails: GuardrailsConfig
    llm_failure: LLMFailureConfig
    digest: DigestConfig
    assets: AssetsConfig
    agents: AgentsConfig
    continuous_monitor: ContinuousMonitorConfig
    profiles: Mapping[Profile, ProfileConfig]
    regimes: Mapping[Regime, RegimeConfig]
    modes: Mapping[Mode, ModeConfig]
    overlays: Mapping[Overlay, PreEventOverlay | StressOverlay]
    run_types: Mapping[RunType, RunTypeConfig]


@dataclass(frozen=True, slots=True)
class RuntimeDimensions:
    """Runtime-resolved identity dimensions passed to the composition resolver.

    The four values the distillation and pipeline-state layers compute per
    invocation. Grouped to match ``LoadedConfig``.
    """

    active_regime: Regime
    active_mode: Mode
    active_overlays: tuple[Overlay, ...]
    firing_trigger: RunType


def compose_config(inputs: LoadedConfig, runtime: RuntimeDimensions) -> ResolvedConfig:
    """Fold the inputs into a frozen ``ResolvedConfig`` snapshot.

    Pure: equal arguments produce equal (and hash-equal) outputs. Inputs are
    grouped into ``LoadedConfig`` (parsed config objects) and
    ``RuntimeDimensions`` (the four per-invocation identity values) so the
    resolver's call surface stays narrow even as the underlying contract
    accepts every name listed in the story-05 spec.
    """
    profile = inputs.profiles[inputs.main.active_profile]
    regime = inputs.regimes[runtime.active_regime]
    mode = inputs.modes[runtime.active_mode]
    run_type = inputs.run_types[runtime.firing_trigger]

    rule_values = _apply_regime(dict(profile.rule_values), regime.multipliers)
    rule_values = _apply_overlays(rule_values, runtime.active_overlays, inputs.overlays)
    rule_values = _close_feature_flags(rule_values, profile.feature_flags)

    agent_token_budgets = _agent_token_budgets_from_profile(profile)
    enabled_agents = tuple(run_type.agents.enabled)
    agent_overrides: dict[AgentName, dict[str, Any]] = {
        agent: dict(override) for agent, override in run_type.agents.overrides.items()
    }
    active_overlay_configs: tuple[PreEventOverlay | StressOverlay, ...] = tuple(
        inputs.overlays[name] for name in runtime.active_overlays
    )

    return ResolvedConfig(
        profile=profile,
        profile_label=inputs.main.active_profile.value,
        regime=regime,
        regime_label=runtime.active_regime.value,
        mode=mode,
        active_overlays=active_overlay_configs,
        run_type=run_type,
        feature_flags=profile.feature_flags,
        rule_values=_frozen_mapping(rule_values),
        agent_token_budgets=_frozen_mapping(agent_token_budgets),
        enabled_agents=enabled_agents,
        agent_overrides=_frozen_overrides(agent_overrides),
        analyst_output_mode=mode.analyst.output_mode,
        strategist_output_mode=mode.strategist.output_mode,
        strategist_allowed_actions=tuple(mode.strategist.allowed_actions),
        pending_orders_default=mode.strategist.pending_orders_default,
        pm_allowed_command_types=tuple(mode.pm.allowed_command_types),
        pm_emphasis=mode.pm.emphasis,
        news_digest_top_n_per_sector=run_type.qualitative_researcher.news_digest.top_n_per_sector,
        news_digest_top_n_high_priority=run_type.qualitative_researcher.news_digest.top_n_high_priority,
        paths=inputs.main.paths,
        execution_mode=inputs.main.execution_mode,
        scheduler=inputs.scheduler,
        venue=inputs.venue,
        execution=inputs.execution,
        guardrails=inputs.guardrails,
        llm_failure=inputs.llm_failure,
        digest=inputs.digest,
        assets=inputs.assets,
        agents=inputs.agents,
        continuous_monitor=inputs.continuous_monitor,
    )
