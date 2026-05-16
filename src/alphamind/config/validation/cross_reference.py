"""Cross-reference validation across the loaded configuration tree (story 06a).

Per ``docs/design/configuration-management.md`` § Validation, this is the
second of three validation layers — it runs after per-file Pydantic parsing
(stories 03* and 04*) and before the semantic self-test (story 06b). Each check
spans multiple YAML files and asserts that every name reference resolves: a
profile's ``rule_values`` keys exist in ``guardrails.yaml``, an agent's
``tools`` allowlist names members of ``REGISTERED_TOOLS``, ``api_key_env``
references resolve to keys in ``.env``, and so on.

The validator is a pure function: the caller (story 08's loader) reads ``.env``
and the tool registry, then passes ``env_keys`` and ``registered_tools`` in.

Failures aggregate. Every check runs to completion; the function raises one
``CrossReferenceError`` carrying every failure as a joined message so operators
editing the YAML see all broken references at once rather than fixing them one
at a time across reload cycles.
"""

from collections.abc import Mapping
from typing import assert_never

from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.config.models.assets import AssetsConfig
from alphamind.config.models.guardrails import GuardrailsConfig
from alphamind.config.models.main import ExecutionMode, MainConfig, Profile
from alphamind.config.models.overlays import (
    Overlay,
    PreEventOverlay,
    StressOverlay,
)
from alphamind.config.models.profiles import ProfileConfig
from alphamind.config.models.regimes import Regime, RegimeConfig
from alphamind.config.models.run_types import RunType, RunTypeConfig
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.config.resolver import LoadedConfig


class CrossReferenceError(Exception):
    """Raised when one or more cross-reference checks fail.

    The message joins every per-check failure with a newline so the operator
    sees the complete diff between YAML state and reference targets in one
    pass.
    """


def validate_cross_references(
    inputs: LoadedConfig,
    *,
    env_keys: frozenset[str],
    registered_tools: frozenset[str],
) -> None:
    """Run every cross-reference check; raise once if any fail.

    Aggregates failure messages across all checks before raising so the
    operator can fix every broken reference in a single edit pass.
    """
    failures: list[str] = []
    failures.extend(_check_active_profile(inputs.main, inputs.profiles))
    failures.extend(_check_active_sectors(inputs.profiles, inputs.assets))
    failures.extend(_check_profile_rule_values(inputs.profiles, inputs.guardrails))
    failures.extend(_check_regime_multipliers(inputs.regimes, inputs.guardrails))
    failures.extend(_check_regime_rule_coverage(inputs.regimes, inputs.profiles))
    failures.extend(_check_overlay_multipliers(inputs.overlays, inputs.guardrails))
    failures.extend(_check_venue_env_refs(inputs.venue, inputs.main.execution_mode, env_keys))
    failures.extend(_check_agent_tools(inputs.agents, registered_tools))
    failures.extend(_check_scheduler_run_type_files(inputs.scheduler, inputs.run_types))
    failures.extend(_check_run_type_files_cover_enum_members(inputs.run_types))
    failures.extend(_check_run_type_enabled_agents(inputs.run_types, inputs.agents))
    failures.extend(_check_run_type_overrides(inputs.run_types, inputs.agents))

    if failures:
        raise CrossReferenceError("\n".join(failures))


# ---------------------------------------------------------------------------
# Individual checks — each returns a list of failure messages (empty on pass).
# ---------------------------------------------------------------------------


def _check_active_profile(main: MainConfig, profiles: Mapping[Profile, ProfileConfig]) -> list[str]:
    """Check 1: ``main.active_profile`` is loaded."""
    if main.active_profile not in profiles:
        return [
            f"main.yaml: active_profile {main.active_profile.value!r} has no matching "
            f"entry in the loaded profiles bundle (loaded: "
            f"{sorted(p.value for p in profiles)})"
        ]
    return []


def _check_active_sectors(
    profiles: Mapping[Profile, ProfileConfig], assets: AssetsConfig
) -> list[str]:
    """Check 2: every active sector in every profile is a key in assets.sectors."""
    failures: list[str] = []
    sector_keys = set(assets.sectors)
    for profile, config in profiles.items():
        for sector in config.active_sectors:
            if sector not in sector_keys:
                failures.append(
                    f"profiles/{profile.value}.yaml: active_sectors entry {sector!r} "
                    f"is not a key in assets.yaml sectors (have: {sorted(sector_keys)})"
                )
    return failures


def _check_profile_rule_values(
    profiles: Mapping[Profile, ProfileConfig], guardrails: GuardrailsConfig
) -> list[str]:
    """Check 3: every rule_values key in every profile exists in guardrails."""
    failures: list[str] = []
    rule_ids = {rule.id for rule in guardrails.rules}
    for profile, config in profiles.items():
        for rule_id in config.rule_values:
            if rule_id not in rule_ids:
                failures.append(
                    f"profiles/{profile.value}.yaml: rule_values key {rule_id!r} "
                    f"is not a registered rule id in guardrails.yaml"
                )
    return failures


def _check_regime_multipliers(
    regimes: Mapping[Regime, RegimeConfig], guardrails: GuardrailsConfig
) -> list[str]:
    """Check 4: every multiplier key in every regime exists in guardrails."""
    failures: list[str] = []
    rule_ids = {rule.id for rule in guardrails.rules}
    for regime, config in regimes.items():
        for rule_id in config.multipliers:
            if rule_id not in rule_ids:
                failures.append(
                    f"regimes/{regime.value}.yaml: multipliers key {rule_id!r} "
                    f"is not a registered rule id in guardrails.yaml"
                )
    return failures


def _check_regime_rule_coverage(
    regimes: Mapping[Regime, RegimeConfig],
    profiles: Mapping[Profile, ProfileConfig],
) -> list[str]:
    """Check 5: every rule used by any profile is covered by every regime.

    Regime multiplier tables are *complete* (cover every rule used by any
    profile) so the resolver's profile-base x regime-multiplier fold cannot
    raise on a missing key.
    """
    failures: list[str] = []
    rules_in_use: set[str] = set()
    for profile_config in profiles.values():
        rules_in_use.update(profile_config.rule_values)

    for regime, regime_config in regimes.items():
        missing = sorted(rules_in_use - set(regime_config.multipliers))
        for rule_id in missing:
            failures.append(
                f"regimes/{regime.value}.yaml: multipliers missing entry for rule "
                f"{rule_id!r} which is used by at least one profile's rule_values"
            )
    return failures


def _check_overlay_multipliers(
    overlays: Mapping[Overlay, PreEventOverlay | StressOverlay],
    guardrails: GuardrailsConfig,
) -> list[str]:
    """Check 6: every multiplier key in every overlay exists in guardrails."""
    failures: list[str] = []
    rule_ids = {rule.id for rule in guardrails.rules}
    for overlay, config in overlays.items():
        for rule_id in config.multipliers:
            if rule_id not in rule_ids:
                failures.append(
                    f"overlays/{overlay.value}.yaml: multipliers key {rule_id!r} "
                    f"is not a registered rule id in guardrails.yaml"
                )
    return failures


def _check_venue_env_refs(
    venue: VenueConfig,
    execution_mode: ExecutionMode,
    env_keys: frozenset[str],
) -> list[str]:
    """Check 7: ``api_key_env`` / ``api_secret_env`` for the active mode are in ``env_keys``.

    Only the credential block matching ``main.execution_mode`` is enforced — a
    paper-mode operator with no ``ALPACA_LIVE_*`` keys should not trip the check
    over credentials the runtime will never read.
    """
    if execution_mode is ExecutionMode.paper:
        active_creds = venue.alpaca.paper
    elif execution_mode is ExecutionMode.live:
        active_creds = venue.alpaca.live
    else:
        assert_never(execution_mode)
    label = execution_mode.value
    failures: list[str] = []
    for field_name in ("api_key_env", "api_secret_env"):
        env_var = getattr(active_creds, field_name)
        if env_var not in env_keys:
            failures.append(
                f"venue.yaml: alpaca.{label}.{field_name} references "
                f"{env_var!r} which is not present in .env"
            )
    return failures


def _check_agent_tools(agents: AgentsConfig, registered_tools: frozenset[str]) -> list[str]:
    """Check 8: every tool an agent references is in ``registered_tools``.

    Covers both the ``tools`` allowlist on every agent and, for the adaptive
    researcher, the ``tool_caps`` keys.
    """
    failures: list[str] = []
    for name, entry in agents.agents.items():
        failures.extend(_check_agent_tools_list(name, entry, registered_tools))
        if isinstance(entry, AdaptiveAgentConfig):
            failures.extend(_check_agent_tool_caps(name, entry, registered_tools))
    return failures


def _check_agent_tools_list(
    name: AgentName,
    entry: BaseAgentConfig | AdaptiveAgentConfig,
    registered_tools: frozenset[str],
) -> list[str]:
    return [
        f"agents.yaml: agent {name.value!r} tools entry {tool!r} "
        f"is not a member of REGISTERED_TOOLS"
        for tool in entry.tools
        if tool not in registered_tools
    ]


def _check_agent_tool_caps(
    name: AgentName,
    entry: AdaptiveAgentConfig,
    registered_tools: frozenset[str],
) -> list[str]:
    return [
        f"agents.yaml: agent {name.value!r} tool_caps key {tool!r} "
        f"is not a member of REGISTERED_TOOLS"
        for tool in entry.tool_caps
        if tool not in registered_tools
    ]


def _check_scheduler_run_type_files(
    scheduler: SchedulerConfig, run_types: Mapping[RunType, RunTypeConfig]
) -> list[str]:
    """Check 9: every trigger key has a matching run-type file loaded."""
    failures: list[str] = []
    loaded_run_type_keys = {member.value for member in run_types}
    for trigger_key in scheduler.triggers:
        if trigger_key not in loaded_run_type_keys:
            failures.append(
                f"scheduler.yaml: triggers key {trigger_key!r} does not have a "
                f"matching run_types/{trigger_key}.yaml in the loaded bundle"
            )
    return failures


def _check_run_type_files_cover_enum_members(
    run_types: Mapping[RunType, RunTypeConfig],
) -> list[str]:
    """Check 9b: every ``RunType`` enum member has a matching run-type entry.

    The inverse of check 9 for ``RunType.emergency`` — which has no
    ``scheduler.yaml`` cron entry but must still ship a
    ``run_types/emergency.yaml`` so the resolver's
    ``inputs.run_types[runtime.firing_trigger]`` lookup succeeds when the
    emergency receiver task (story 04b) dispatches a ``RunType.emergency``
    invocation.
    """
    return [
        f"run_types: enum member {member.value!r} has no matching "
        f"run_types/{member.value}.yaml entry in the loaded bundle"
        for member in RunType
        if member not in run_types
    ]


def _check_run_type_enabled_agents(
    run_types: Mapping[RunType, RunTypeConfig], agents: AgentsConfig
) -> list[str]:
    """Check 10: every enabled agent in every run-type exists in agents.yaml.

    Already enforced at parse time by the closed ``AgentName`` enum; re-asserted
    here as defensive coverage for hand-crafted Pydantic instances that bypass
    YAML parsing.
    """
    failures: list[str] = []
    known_agents = set(agents.agents)
    for rt, config in run_types.items():
        for agent in config.agents.enabled:
            if agent not in known_agents:
                failures.append(
                    f"run_types/{rt.value}.yaml: agents.enabled entry {agent.value!r} "
                    f"is not present in agents.yaml"
                )
    return failures


def _check_run_type_overrides(
    run_types: Mapping[RunType, RunTypeConfig], agents: AgentsConfig
) -> list[str]:
    """Check 11: every override field is a recognized field on the agent's model.

    Already enforced at parse time on ``AgentsSection`` per story 04e (against a
    narrower override allow-list); re-asserted here defensively against the
    underlying Pydantic model field set so a future bypass of parse-time
    validation still catches the divergence.
    """
    failures: list[str] = []
    for rt, run_type_config in run_types.items():
        for agent, override in run_type_config.agents.overrides.items():
            agent_entry = agents.agents.get(agent)
            if agent_entry is None:
                # Reported by check 10; nothing more to say here.
                continue
            agent_field_names = set(type(agent_entry).model_fields)
            for field_name in override:
                if field_name not in agent_field_names:
                    failures.append(
                        f"run_types/{rt.value}.yaml: agents.overrides.{agent.value} "
                        f"field {field_name!r} is not a field on "
                        f"{type(agent_entry).__name__}"
                    )
    return failures
