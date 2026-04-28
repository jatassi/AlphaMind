"""Semantic self-test invariants for the configuration tree (story 06b).

The third validation layer per ``docs/design/configuration-management.md``
§ Validation. Sits on top of parse-time typing (each Pydantic model in
``models/``) and cross-reference name resolution (story 06a). Asserts the
runtime-meaningful invariants — drawdown progressive tiers monotonic, no
regime multiplier drives a rule limit to zero or negative for any profile,
escalation zones ordered, capital ranges non-overlapping, ticker uniqueness
across sectors and benchmarks, ``last_full_validation`` no later than today,
feature-flag closure (no residue), and the ``digest.yaml`` numerical sanity
invariants — that would parse-validate and cross-reference-validate cleanly
but produce wrong behavior at runtime.

Pure: ``today: date`` is a parameter, not ``date.today()``. The validator
accepts a precomputed ``composed_configs`` map so it stays self-contained;
``enumerate_compositions`` is a sibling helper that builds the map from a
``LoadedConfig`` by invoking the resolver across the full matrix.

Failures aggregate: every invariant violation is collected and reported in a
single ``SemanticInvariantError`` so operators editing the YAML see every
issue at once rather than one-at-a-time fix-and-rerun.
"""

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from itertools import combinations, pairwise

from alphamind.config.models.assets import AssetsConfig
from alphamind.config.models.digest import DigestConfig
from alphamind.config.models.guardrails import GuardrailsConfig
from alphamind.config.models.main import Profile
from alphamind.config.models.modes import Mode
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.profiles import ProfileConfig
from alphamind.config.models.regimes import Regime, RegimeConfig
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import (
    LoadedConfig,
    ResolvedConfig,
    RuntimeDimensions,
    compose_config,
)

# Closed set of options-related rule IDs. Re-asserts the resolver's
# feature-flag closure: when a profile carries ``options_enabled: false``,
# none of these may appear in the resolved ``rule_values``.
OPTIONS_RULE_SET: frozenset[str] = frozenset(
    {
        "position_max_loss_options_pct",
        "options_delta_pct",
        "portfolio_theta_pct_per_day",
        "portfolio_vega_pct_per_iv_point",
    }
)

# Closed set of shorts-related rule IDs. ``gross_exposure_pct`` is **not**
# included: it is binding even with shorts disabled (gross == net long in that
# case, but the rule itself remains in scope).
SHORTS_RULE_SET: frozenset[str] = frozenset(
    {
        "net_short_pct",
        "total_short_pct",
        "single_short_max_pct",
        "borrow_cost_budget_pct_per_day",
    }
)

_OPTIONS_RULE_PREFIXES: tuple[str, ...] = ("options_", "portfolio_theta_", "portfolio_vega_")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")

_PROGRESSIVE_TIER_RULE_ID = "cumulative_drawdown_pct"

CompositionKey = tuple[Profile, Regime, Mode, tuple[Overlay, ...], RunType]


class SemanticInvariantError(Exception):
    """Raised when one or more semantic invariants are violated.

    Aggregates every violation into one exception; the message names each
    offending field and the rule that fired.
    """


def enumerate_compositions(
    loaded: LoadedConfig,
) -> dict[CompositionKey, ResolvedConfig]:
    """Compose every (profile, regime, mode, overlay-subset, run-type) combination.

    The matrix is bounded — 4 profiles times 4 regimes times 2 modes times 4
    overlay subsets times 6 run-types equals 768 entries — and each
    per-composition cost is O(rule count). Designed to run once per config load.

    Returns a mapping keyed by the runtime-dimension tuple plus the profile
    that drove the cascade. Story 08's loader is the typical caller; tests
    consume the map directly.
    """
    overlay_subsets = _overlay_subsets(tuple(loaded.overlays.keys()))
    composed: dict[CompositionKey, ResolvedConfig] = {}
    for profile_name in loaded.profiles:
        loaded_for_profile = replace(
            loaded, main=loaded.main.model_copy(update={"active_profile": profile_name})
        )
        for regime_name in loaded.regimes:
            for mode_name in loaded.modes:
                for overlays in overlay_subsets:
                    for run_type_name in loaded.run_types:
                        runtime = RuntimeDimensions(
                            active_regime=regime_name,
                            active_mode=mode_name,
                            active_overlays=overlays,
                            firing_trigger=run_type_name,
                        )
                        key: CompositionKey = (
                            profile_name,
                            regime_name,
                            mode_name,
                            overlays,
                            run_type_name,
                        )
                        composed[key] = compose_config(loaded_for_profile, runtime)
    return composed


def _overlay_subsets(overlays: tuple[Overlay, ...]) -> tuple[tuple[Overlay, ...], ...]:
    """Every subset of ``overlays`` in canonical (input) order.

    The empty tuple is included; subsets preserve the input order so callers
    can distinguish ``(pre_event, stress)`` from ``(stress, pre_event)`` —
    overlay arithmetic is commutative but the resolver records order for
    review-surface stability.
    """
    subsets: list[tuple[Overlay, ...]] = [()]
    for size in range(1, len(overlays) + 1):
        for combo in combinations(overlays, size):
            subsets.append(combo)
    return tuple(subsets)


def validate_semantic_invariants(
    *,
    guardrails: GuardrailsConfig,
    assets: AssetsConfig,
    digest: DigestConfig,
    profiles: Mapping[Profile, ProfileConfig],
    regimes: Mapping[Regime, RegimeConfig],
    composed_configs: Mapping[CompositionKey, ResolvedConfig],
    today: date,
) -> None:
    """Aggregate every semantic invariant violation into one error.

    Raises ``SemanticInvariantError`` once with every failure listed if any
    invariant fires. Returns ``None`` on a clean tree.
    """
    failures: list[str] = []

    failures.extend(_check_progressive_tiers_monotonic(guardrails))
    failures.extend(_check_regime_multipliers_positive(regimes))
    failures.extend(_check_resolved_rule_values_positive(composed_configs))
    failures.extend(_check_escalation_zones_ordered(guardrails))
    failures.extend(_check_capital_ranges_non_overlapping(profiles))
    failures.extend(_check_ticker_uniqueness_and_format(assets))
    failures.extend(_check_active_sectors_non_empty(profiles, assets))
    failures.extend(_check_last_full_validation(assets, today))
    failures.extend(_check_feature_flag_closure(composed_configs))
    failures.extend(_check_digest_numerical_bounds(digest))

    if failures:
        raise SemanticInvariantError("\n".join(failures))


def _check_progressive_tiers_monotonic(guardrails: GuardrailsConfig) -> list[str]:
    failures: list[str] = []
    for rule in guardrails.rules:
        if rule.id != _PROGRESSIVE_TIER_RULE_ID or not rule.progressive_tiers:
            continue
        triggers = [tier.trigger_pct for tier in rule.progressive_tiers]
        if any(b <= a for a, b in pairwise(triggers)):
            failures.append(
                f"guardrails.rules[{rule.id!r}].progressive_tiers: "
                f"trigger_pct must be strictly increasing, got {triggers}"
            )
    return failures


def _check_regime_multipliers_positive(
    regimes: Mapping[Regime, RegimeConfig],
) -> list[str]:
    """Every multiplier in every regime is strictly positive.

    Already enforced at parse time by ``RegimeConfig.multipliers_are_well_formed``
    (story 04b); re-asserted defensively here so that any edit bypassing parse
    validation is still caught. The check is one O(rules) scan per regime,
    which is negligible.
    """
    failures: list[str] = []
    for regime_name, regime in regimes.items():
        for rule_id, multiplier in regime.multipliers.items():
            if multiplier <= 0:
                failures.append(
                    f"regimes[{regime_name.value!r}].multipliers[{rule_id!r}] = "
                    f"{multiplier}; multipliers must be > 0 (zero or negative would "
                    f"invert the rule)"
                )
    return failures


def _check_resolved_rule_values_positive(
    composed_configs: Mapping[CompositionKey, ResolvedConfig],
) -> list[str]:
    """Every rule limit in every composed snapshot is strictly positive."""
    failures: list[str] = []
    for key, resolved in composed_configs.items():
        for rule_id, value in resolved.rule_values.items():
            if value <= 0:
                profile, regime, mode, overlays, run_type = key
                failures.append(
                    f"composed_configs[(profile={profile.value}, regime={regime.value}, "
                    f"mode={mode.value}, overlays={tuple(o.value for o in overlays)}, "
                    f"run_type={run_type.value})].rule_values[{rule_id!r}] = {value}; "
                    f"resolved rule limits must be > 0"
                )
    return failures


def _check_escalation_zones_ordered(guardrails: GuardrailsConfig) -> list[str]:
    failures: list[str] = []
    for rule in guardrails.rules:
        z = rule.escalation_zones
        if not (z.warning < z.critical < z.hard_block):
            failures.append(
                f"guardrails.rules[{rule.id!r}].escalation_zones: "
                f"warning < critical < hard_block required, "
                f"got {z.warning}/{z.critical}/{z.hard_block}"
            )
    return failures


def _check_capital_ranges_non_overlapping(
    profiles: Mapping[Profile, ProfileConfig],
) -> list[str]:
    failures: list[str] = []
    for (name_a, profile_a), (name_b, profile_b) in combinations(profiles.items(), 2):
        a_lower, a_upper = profile_a.capital_range_usd
        b_lower, b_upper = profile_b.capital_range_usd
        if a_lower <= b_upper and b_lower <= a_upper:
            failures.append(
                f"profiles {name_a.value!r} capital_range_usd {[a_lower, a_upper]} "
                f"overlaps profiles {name_b.value!r} capital_range_usd "
                f"{[b_lower, b_upper]}; capital ranges must be disjoint"
            )
    return failures


def _check_ticker_uniqueness_and_format(assets: AssetsConfig) -> list[str]:
    failures: list[str] = []
    seen: dict[str, str] = {}
    for sector_name, tickers in assets.sectors.items():
        for ticker in tickers:
            if not _TICKER_RE.match(ticker):
                failures.append(
                    f"assets.sectors[{sector_name!r}] ticker {ticker!r} does not match "
                    f"{_TICKER_RE.pattern}"
                )
            origin = f"sectors[{sector_name!r}]"
            if ticker in seen:
                failures.append(
                    f"ticker {ticker!r} appears in both {seen[ticker]} and {origin}; "
                    f"tickers must be unique across sectors and benchmarks"
                )
            else:
                seen[ticker] = origin
    for ticker in assets.benchmarks:
        if not _TICKER_RE.match(ticker):
            failures.append(f"assets.benchmarks key {ticker!r} does not match {_TICKER_RE.pattern}")
        origin = "benchmarks"
        if ticker in seen:
            failures.append(
                f"ticker {ticker!r} appears in both {seen[ticker]} and {origin}; "
                f"tickers must be unique across sectors and benchmarks"
            )
        else:
            seen[ticker] = origin
    return failures


def _check_active_sectors_non_empty(
    profiles: Mapping[Profile, ProfileConfig],
    assets: AssetsConfig,
) -> list[str]:
    failures: list[str] = []
    for profile_name, profile in profiles.items():
        for sector in profile.active_sectors:
            tickers = assets.sectors.get(sector)
            if tickers is None:
                # Cross-reference layer (story 06a) catches missing sectors;
                # the semantic layer skips silently to keep the failure vector
                # focused on emptiness.
                continue
            if not tickers:
                failures.append(
                    f"profiles[{profile_name.value!r}].active_sectors entry {sector!r} "
                    f"has empty ticker list in assets.sectors; sectors used by a profile "
                    f"must have at least one ticker"
                )
    return failures


def _check_last_full_validation(assets: AssetsConfig, today: date) -> list[str]:
    if assets.last_full_validation is None:
        return []
    if assets.last_full_validation > today:
        return [
            f"assets.last_full_validation = {assets.last_full_validation.isoformat()} "
            f"is after today ({today.isoformat()}); validation date must be on or "
            f"before today"
        ]
    return []


def _check_feature_flag_closure(
    composed_configs: Mapping[CompositionKey, ResolvedConfig],
) -> list[str]:
    """No options/shorts rule survives in a profile that disabled the feature."""
    failures: list[str] = []
    for key, resolved in composed_configs.items():
        flags = resolved.feature_flags
        for rule_id in resolved.rule_values:
            if not flags.options_enabled and _is_options_rule(rule_id):
                failures.append(_closure_message(key, rule_id, flag="options_enabled"))
            if not flags.short_selling_enabled and rule_id in SHORTS_RULE_SET:
                failures.append(_closure_message(key, rule_id, flag="short_selling_enabled"))
    return failures


def _is_options_rule(rule_id: str) -> bool:
    return rule_id in OPTIONS_RULE_SET or rule_id.startswith(_OPTIONS_RULE_PREFIXES)


def _closure_message(key: CompositionKey, rule_id: str, *, flag: str) -> str:
    profile, regime, mode, overlays, run_type = key
    return (
        f"composed_configs[(profile={profile.value}, regime={regime.value}, "
        f"mode={mode.value}, overlays={tuple(o.value for o in overlays)}, "
        f"run_type={run_type.value})].rule_values contains {rule_id!r} but the "
        f"profile has {flag} disabled; feature-flag closure requires the rule "
        f"be dropped from the resolved config"
    )


def _check_digest_numerical_bounds(digest: DigestConfig) -> list[str]:
    failures: list[str] = []
    if digest.anti_pattern_spike.baseline_window_weeks < 1:
        failures.append(
            f"digest.anti_pattern_spike.baseline_window_weeks must be >= 1, "
            f"got {digest.anti_pattern_spike.baseline_window_weeks}"
        )
    if digest.anti_pattern_spike.multiplier_vs_baseline <= 1.0:
        failures.append(
            f"digest.anti_pattern_spike.multiplier_vs_baseline must be > 1.0, "
            f"got {digest.anti_pattern_spike.multiplier_vs_baseline}"
        )
    if digest.anti_pattern_spike.min_occurrences_this_week < 0:
        failures.append(
            f"digest.anti_pattern_spike.min_occurrences_this_week must be >= 0, "
            f"got {digest.anti_pattern_spike.min_occurrences_this_week}"
        )
    if digest.sector_underperform.baseline_window_weeks < 1:
        failures.append(
            f"digest.sector_underperform.baseline_window_weeks must be >= 1, "
            f"got {digest.sector_underperform.baseline_window_weeks}"
        )
    if digest.sector_underperform.median_offset_sigma <= 0:
        failures.append(
            f"digest.sector_underperform.median_offset_sigma must be > 0, "
            f"got {digest.sector_underperform.median_offset_sigma}"
        )
    for shift_name, threshold in (
        ("citation_chain_shift", digest.citation_chain_shift),
        ("source_signal_survival_drop", digest.source_signal_survival_drop),
    ):
        if threshold.baseline_window_weeks < 1:
            failures.append(
                f"digest.{shift_name}.baseline_window_weeks must be >= 1, "
                f"got {threshold.baseline_window_weeks}"
            )
        if not 0 < threshold.delta_pp_threshold <= 100:
            failures.append(
                f"digest.{shift_name}.delta_pp_threshold must be in (0, 100], "
                f"got {threshold.delta_pp_threshold}"
            )
    if digest.validation_window_end.days_before_due < 0:
        failures.append(
            f"digest.validation_window_end.days_before_due must be >= 0, "
            f"got {digest.validation_window_end.days_before_due}"
        )
    return failures
