"""Guardrail enforcement layer end-to-end verification (ALP-398).

Operator entry point (via thin shim at ``scripts/verify_guardrail_enforcement.py``).
Exercises the Phase 1 enforcement orchestrator end-to-end against a freshly-
migrated SQLite DB across four phases:

* Phase 1 — composition primitive. Drives ``compose_active_risk_parameters``
  across the documented tier cases (no-tier, ``CONSTRAINED``,
  ``HEAVILY_CONSTRAINED``, ``FULL_HALT``); each case asserts the returned
  ``(ActiveRiskParameterSet, DrawdownTier | None)`` matches expectations.
* Phase 2 — orchestrator. Wraps a synthetic ``RegimeAdaptationOutput`` plus a
  per-tier ``DrawdownState`` and calls ``compose_phase_1_enforcement``;
  asserts the bundled ``Phase1EnforcementResult`` matches expectations.
* Phase 3 — repository provider. Builds a ``SqlPortfolioStateRepository``
  against the freshly-migrated DB using ``make_active_risk_parameters_provider``
  for the active provider slot; asserts ``get_active_risk_parameters()``
  returns the bundled result's parameters unchanged.
* Phase 4 — assembler integration. Uses the same repository to build a
  portfolio-state snapshot via ``assemble_snapshot``; asserts
  ``snapshot.active_risk_parameters`` equals the composed result modulo the
  documented assembler ``parameter_change_flag`` enrichment.

No SDK invocation; no live broker contact. Sub-second runtime against a fresh
on-disk DB.

Usage::

    uv run python scripts/verify_guardrail_enforcement.py [--db-path PATH] [--output FORMAT]

See ``scripts/RUNBOOK_guardrail_enforcement.md`` for the operator runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.config.guardrails_helpers import (
    load_cumulative_drawdown_progressive_tiers,
)
from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.config.models.regimes import Regime
from alphamind.execution.guardrail_enforcement import (
    Phase1EnforcementResult,
    compose_active_risk_parameters,
    compose_phase_1_enforcement,
    make_active_risk_parameters_provider,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.pricing import StubCurrentPriceProvider
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.portfolio_state.repository import RepositoryConsistencyError
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationOutput,
    RegimeAdaptationState,
)
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.repository import (
    build_sql_portfolio_state_repository,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)

__all__ = [
    "PhaseResult",
    "main",
    "run_phase_1_composition_primitive",
    "run_phase_2_orchestrator",
    "run_phase_3_repository_provider",
    "run_phase_4_assembler_integration",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseResult:
    """One phase's outcome — pass/fail flag + named label + diagnostic detail."""

    label: str
    ok: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Shared fixture constants — kept narrow and stable so the verify script's
# diagnostic output is deterministic across runs.
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 9, 14, 30, 0, tzinfo=UTC)
_PROCESS_ID = "verify-guardrail-enforcement-proc"
_INV_ID_BASE = "verify-guardrail-enforcement-inv"


def _baseline_normal_parameters() -> ActiveRiskParameterSet:
    """Construct a normal-regime parameter set carrying the rules tier overrides target.

    Both ``position_max_size_pct`` and ``gross_exposure_pct`` are required — the
    tier-1/tier-2 override applier raises if either is absent. Baseline values
    are loose enough that every tier configured in the shipped guardrails
    config strictly tightens them (proves the override applied).
    """
    entries = (
        ActiveRiskParameterEntry(
            rule_id="position_max_size_pct",
            rule_label="position_max_size_pct",
            value=5.0,
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=5.0,
        ),
        ActiveRiskParameterEntry(
            rule_id="gross_exposure_pct",
            rule_label="gross_exposure_pct",
            value=120.0,
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=120.0,
        ),
    )
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


def _drawdown_state(*, current_drawdown_pct: float) -> DrawdownState:
    """Construct a ``DrawdownState`` with the named cumulative drawdown level.

    Other fields take stable defaults so phase output diagnostics stay
    deterministic — only ``current_drawdown_pct`` varies across tier cases.
    """
    return DrawdownState(
        current_drawdown_pct=current_drawdown_pct,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _regime_adaptation_state() -> RegimeAdaptationState:
    """Synthesize a stable-regime ``RegimeAdaptationState`` for the orchestrator output."""
    return RegimeAdaptationState(
        as_of=_NOW.isoformat().replace("+00:00", "Z"),
        invocation_id=_INV_ID_BASE,
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="normal",
        distillation_vix_level=18.0,
        regime_skip_emergency=False,
    )


def _regime_output(*, parameters: ActiveRiskParameterSet) -> RegimeAdaptationOutput:
    """Wrap the given parameter set in a synthetic stable-regime adaptation output."""
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=parameters,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=_regime_adaptation_state(),
        audit_log_entries=(),
    )


# ---------------------------------------------------------------------------
# Phase 1 — composition primitive
# ---------------------------------------------------------------------------


_PHASE_1_LABEL = "Phase 1 — composition primitive"

_TIER_OVERLAY_NUMBER: dict[DrawdownTier, int] = {
    DrawdownTier.CONSTRAINED: 1,
    DrawdownTier.HEAVILY_CONSTRAINED: 2,
    DrawdownTier.FULL_HALT: 3,
}


def _tier_cases(
    tiers: tuple[ProgressiveTier, ...],
) -> tuple[tuple[float, DrawdownTier | None, str], ...]:
    """Return the canonical (drawdown_pct, expected_tier, label) tuples Phase 1 + 2 share."""
    non_halt = tuple(t for t in tiers if not t.full_halt)
    full_halt = next(t for t in tiers if t.full_halt)
    return (
        (0.0, None, "no-tier (zero drawdown)"),
        (non_halt[0].trigger_pct, DrawdownTier.CONSTRAINED, "tier 1 / CONSTRAINED"),
        (
            non_halt[1].trigger_pct,
            DrawdownTier.HEAVILY_CONSTRAINED,
            "tier 2 / HEAVILY_CONSTRAINED",
        ),
        (full_halt.trigger_pct, DrawdownTier.FULL_HALT, "tier 3 / FULL_HALT"),
    )


def run_phase_1_composition_primitive() -> PhaseResult:
    """Drive the composition primitive across the four documented tier cases.

    For each case the primitive's returned ``(ActiveRiskParameterSet,
    DrawdownTier | None)`` is asserted against the expected tier and override
    semantics. Tier triggers loaded from ``config/guardrails.yaml`` so this
    phase exercises the same data the production orchestrator consumes.
    """
    tiers = load_cumulative_drawdown_progressive_tiers()
    baseline = _baseline_normal_parameters()
    for current_pct, expected_tier, label in _tier_cases(tiers):
        params, tier = compose_active_risk_parameters(
            regime_resolved_parameters=baseline,
            drawdown_state=_drawdown_state(current_drawdown_pct=current_pct),
            progressive_tiers=tiers,
        )
        if tier != expected_tier:
            return PhaseResult(
                label=_PHASE_1_LABEL,
                ok=False,
                detail=f"{label}: tier={tier!r}, expected {expected_tier!r}",
            )
        if expected_tier is None and params is not baseline:
            return PhaseResult(
                label=_PHASE_1_LABEL,
                ok=False,
                detail=f"{label}: parameters not pass-through (no tier should be a no-op)",
            )
        if expected_tier is not None:
            overlay_tag = f"cumulative_drawdown_tier_{_TIER_OVERLAY_NUMBER[expected_tier]}"
            if overlay_tag not in params.active_overlays:
                return PhaseResult(
                    label=_PHASE_1_LABEL,
                    ok=False,
                    detail=(
                        f"{label}: missing overlay tag {overlay_tag!r} in {params.active_overlays}"
                    ),
                )

    return PhaseResult(label=_PHASE_1_LABEL, ok=True)


# ---------------------------------------------------------------------------
# Phase 2 — orchestrator
# ---------------------------------------------------------------------------


_PHASE_2_LABEL = "Phase 2 — orchestrator"


def run_phase_2_orchestrator() -> PhaseResult:
    """Drive the orchestrator across the four documented tier cases.

    Wraps the baseline parameter set in a synthetic ``RegimeAdaptationOutput``
    and pairs it with a ``DrawdownState`` for each tier; asserts the bundled
    ``Phase1EnforcementResult`` carries the expected tier and parameters that
    match the underlying primitive's output.
    """
    tiers = load_cumulative_drawdown_progressive_tiers()
    baseline = _baseline_normal_parameters()
    regime_out = _regime_output(parameters=baseline)
    for current_pct, expected_tier, label in _tier_cases(tiers):
        result = compose_phase_1_enforcement(
            regime_output=regime_out,
            drawdown_state=_drawdown_state(current_drawdown_pct=current_pct),
            progressive_tiers=tiers,
        )
        if not isinstance(result, Phase1EnforcementResult):
            return PhaseResult(
                label=_PHASE_2_LABEL,
                ok=False,
                detail=(
                    f"{label}: orchestrator returned {type(result).__name__!r}, "
                    f"expected Phase1EnforcementResult"
                ),
            )
        if result.drawdown_tier != expected_tier:
            return PhaseResult(
                label=_PHASE_2_LABEL,
                ok=False,
                detail=(
                    f"{label}: drawdown_tier={result.drawdown_tier!r}, expected {expected_tier!r}"
                ),
            )

        # Cross-check vs. the underlying primitive — orchestrator must not
        # strip or duplicate primitive output.
        expected_params, _ = compose_active_risk_parameters(
            regime_resolved_parameters=baseline,
            drawdown_state=_drawdown_state(current_drawdown_pct=current_pct),
            progressive_tiers=tiers,
        )
        if result.active_risk_parameters != expected_params:
            return PhaseResult(
                label=_PHASE_2_LABEL,
                ok=False,
                detail=(
                    f"{label}: orchestrator parameters disagree with composition primitive output"
                ),
            )

    return PhaseResult(label=_PHASE_2_LABEL, ok=True)


# ---------------------------------------------------------------------------
# Phase 3 — repository provider
# ---------------------------------------------------------------------------


_PHASE_3_LABEL = "Phase 3 — repository provider"


def _state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/verify-guardrail-enforcement",
            "invocation_provenance_root": "/tmp/verify-guardrail-enforcement",
        }
    )


def _open_async_factory(
    db_path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Return (engine, factory) pair the caller is responsible for disposing."""
    engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(engine)
    return engine, factory


def _phase_3_enforcement_result() -> Phase1EnforcementResult:
    """Compose a representative ``Phase1EnforcementResult`` for Phase 3 + 4.

    Wraps the baseline parameter set in a synthetic ``RegimeAdaptationOutput``
    paired with a zero-drawdown ``DrawdownState``: the no-tier branch yields a
    pass-through (``parameter_change_flag=False``, no overlays) — the cleanest
    fixture for the assembler-integration phase.
    """
    return compose_phase_1_enforcement(
        regime_output=_regime_output(parameters=_baseline_normal_parameters()),
        drawdown_state=_drawdown_state(current_drawdown_pct=0.0),
        progressive_tiers=load_cumulative_drawdown_progressive_tiers(),
    )


async def run_phase_3_repository_provider(db_path: Path) -> PhaseResult:
    """Build a ``SqlPortfolioStateRepository`` against a freshly-migrated DB.

    Plug ``make_active_risk_parameters_provider(result)`` into the active
    provider slot; assert ``get_active_risk_parameters()`` returns the bundled
    result's parameters unchanged (object identity).

    No schema check — ``get_active_risk_parameters`` is a pure delegate to the
    provider closure and does not read the DB. Phase 4 catches schema gaps
    where they actually matter.
    """
    result = _phase_3_enforcement_result()
    provider = make_active_risk_parameters_provider(result)

    def _prior_provider(_path: str) -> ActiveRiskParameterSet:
        return _baseline_normal_parameters()

    engine, factory = _open_async_factory(db_path)
    try:
        repository = build_sql_portfolio_state_repository(
            session_factory=factory,
            invocation_id=f"{_INV_ID_BASE}-phase-3",
            active_risk_parameters_provider=provider,
            prior_active_risk_parameters_provider=_prior_provider,
            config=_state_persistence_config(),
        )
        yielded = repository.get_active_risk_parameters()
    finally:
        await engine.dispose()

    if yielded is not result.active_risk_parameters:
        return PhaseResult(
            label=_PHASE_3_LABEL,
            ok=False,
            detail=(
                "repository.get_active_risk_parameters() did not return the "
                "result's active_risk_parameters by identity"
            ),
        )

    return PhaseResult(label=_PHASE_3_LABEL, ok=True)


# ---------------------------------------------------------------------------
# Phase 4 — assembler integration
# ---------------------------------------------------------------------------


_PHASE_4_LABEL = "Phase 4 — assembler integration"

_PHASE_4_REQUIRED_TABLES: tuple[str, ...] = (
    "process_lifetimes",
    "invocations",
    "activity_log",
    "positions",
    "theses",
    "thesis_components",
    "orders",
    "brackets",
    "bracket_legs",
    "cash_ledger",
    "drawdown_state",
    "fill_records",
    "corporate_action_integration_ledger",
)
"""Tables ``assemble_snapshot`` reads through the SQL repository."""


def _missing_tables(db_path: Path, tables: Sequence[str]) -> list[str]:
    from sqlalchemy import inspect

    sync_engine = make_engine(str(db_path))
    try:
        existing = set(inspect(sync_engine).get_table_names())
    finally:
        sync_engine.dispose()
    return [t for t in tables if t not in existing]


def _process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="verify-guardrail-enforcement",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/verify-guardrail-enforcement/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="verify-script",
    )


def _invocation_record(invocation_id: str, *, start_at: datetime | None = None) -> InvocationRecord:
    started = start_at if start_at is not None else _NOW
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=started.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="verify_guardrail_enforcement.py",
        trigger_reason="operator-triggered verification",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/verify-guardrail-enforcement/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/verify-guardrail-enforcement/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def _seed_process_lifetime(factory: async_sessionmaker[AsyncSession]) -> None:
    """Persist the parent ``process_lifetime`` row required by the FK on invocations."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from alphamind.state.tables.process_lifetimes import (
        ProcessLifetimeRow,
    )

    row = process_lifetime_record_to_row(_process_lifetime_record())
    values = {col.name: getattr(row, col.name) for col in ProcessLifetimeRow.__table__.columns}
    stmt = sqlite_insert(ProcessLifetimeRow).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["process_lifetime_id"])
    async with factory() as sess:
        await sess.execute(stmt)
        await sess.commit()


async def _seed_invocation_with_phase1_committed(
    *,
    factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    start_at: datetime,
    phase1_completed_at: datetime,
) -> None:
    """Idempotently insert/update an invocation row with ``phase1_completed_at`` stamped.

    Bypasses ``InvocationContext`` because Phase 4 only needs the row in the
    canonical "Phase 1 committed" state for the SQL repository's snapshot
    integration; the full context lifecycle is exercised in
    ``verify_state_persistence``'s Phase B + Phase F.

    Idempotent so the operator can re-run the verify script against the same
    DB without dropping the schema first; on a re-run the existing row's
    timestamps are refreshed in-place.
    """
    from alphamind.state.tables.invocations import InvocationRow

    record = _invocation_record(invocation_id, start_at=start_at)
    row_template = invocation_record_to_row(record)
    phase1_iso = phase1_completed_at.isoformat().replace("+00:00", "Z")
    async with factory() as sess:
        existing = await sess.get(InvocationRow, invocation_id)
        if existing is None:
            row_template.phase1_completed_at = phase1_iso
            sess.add(row_template)
        else:
            existing.start_at = row_template.start_at
            existing.phase1_completed_at = phase1_iso
        await sess.commit()


async def _seed_singletons(factory: async_sessionmaker[AsyncSession], *, now: datetime) -> None:
    """Idempotently persist the cash_ledger + drawdown_state singletons.

    ``assemble_snapshot`` reads both singletons; the SQL repo raises if either
    is absent. Idempotent so the operator can re-run the verify script against
    the same DB without dropping the schema first.
    """
    from alphamind.state.tables.cash_ledger import (
        CASH_LEDGER_SINGLETON_ID,
        CashLedgerRow,
    )
    from alphamind.state.tables.drawdown_state import (
        DRAWDOWN_STATE_SINGLETON_ID,
        DrawdownStateRow,
    )

    cash = CashLedger(
        current_cash_usd=100_000.0,
        settled_cash_usd=100_000.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=100_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    async with factory() as sess:
        if await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID) is None:
            sess.add(cash_ledger_record_to_row(cash, last_updated_at=now))
        if await sess.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID) is None:
            sess.add(
                drawdown_state_record_to_row(
                    _drawdown_state(current_drawdown_pct=0.0), last_updated_at=now
                )
            )
        await sess.commit()


async def run_phase_4_assembler_integration(db_path: Path) -> PhaseResult:
    """Build a portfolio-state snapshot via ``assemble_snapshot`` and assert
    ``snapshot.active_risk_parameters`` matches the composed result.

    The assembler enriches ``parameter_change_flag`` from the prior provider —
    when prior is None (no earlier invocation), the enrichment is a no-op
    (flag stays False), so the equality check holds modulo no flag drift.
    """
    missing = _missing_tables(db_path, _PHASE_4_REQUIRED_TABLES)
    if missing:
        return PhaseResult(
            label=_PHASE_4_LABEL,
            ok=False,
            detail=f"missing required tables: {', '.join(missing)}",
        )

    result = _phase_3_enforcement_result()
    provider = make_active_risk_parameters_provider(result)

    def _prior_provider(_path: str) -> ActiveRiskParameterSet:
        # The SQL repository only consults this provider when an earlier
        # invocation row exists. With Phase 4's single seeded invocation, the
        # prior-context branch returns prior_active_risk_parameters=None and
        # the assembler's parameter_change_flag enrichment is a no-op.
        return _baseline_normal_parameters()

    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_process_lifetime(factory)

        # Time anchoring mirrors verify_state_persistence Phase F: snapshot's
        # freshness invariant requires phase1_committed_at <= snapshot_assembled_at
        # AND pipeline_invocation_started_at >= snapshot_assembled_at. Stamp
        # both to ``now`` so equality (a degenerate but valid case) holds.
        now = datetime.now(UTC)
        invocation_id = f"{_INV_ID_BASE}-phase-4"
        await _seed_invocation_with_phase1_committed(
            factory=factory,
            invocation_id=invocation_id,
            start_at=now + timedelta(seconds=1),
            phase1_completed_at=now,
        )
        await _seed_singletons(factory, now=now)

        repository = build_sql_portfolio_state_repository(
            session_factory=factory,
            invocation_id=invocation_id,
            active_risk_parameters_provider=provider,
            prior_active_risk_parameters_provider=_prior_provider,
            config=_state_persistence_config(),
        )

        portfolio_config = PortfolioStateConfig(
            pm_decision_log_sliding_window_invocations=5,
            thesis_resolutions_lookback_trading_days=10,
            thesis_quality_aggregates_trailing_windows_days=(5, 20),
            snapshot_freshness_max_phase1_to_snapshot_seconds=300.0,
            snapshot_freshness_max_price_age_seconds=60.0,
        )
        price_provider = StubCurrentPriceProvider({}, now=now)

        def _sector_resolver(_pos: PositionRecord) -> str | None:
            return None

        try:
            assembled = assemble_snapshot(
                repository=repository,
                price_provider=price_provider,
                sector_resolver=_sector_resolver,
                config=portfolio_config,
                now=now,
            )
        except RepositoryConsistencyError as exc:
            return PhaseResult(
                label=_PHASE_4_LABEL,
                ok=False,
                detail=f"assemble_snapshot raised RepositoryConsistencyError: {exc}",
            )

        snapshot = assembled.snapshot
        # The assembler enriches ``parameter_change_flag``; with no prior
        # invocation this is a no-op (flag stays False matching the result).
        # Equality on the full set holds in this configuration.
        if snapshot.active_risk_parameters != result.active_risk_parameters:
            return PhaseResult(
                label=_PHASE_4_LABEL,
                ok=False,
                detail=(
                    "snapshot.active_risk_parameters does not match the "
                    "composed Phase1EnforcementResult.active_risk_parameters"
                ),
            )
    finally:
        await engine.dispose()

    return PhaseResult(label=_PHASE_4_LABEL, ok=True)


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _render_text(results: Sequence[PhaseResult]) -> str:
    lines = ["=" * 70, "AlphaMind Guardrail Enforcement Verification", "=" * 70]
    pass_count = sum(1 for r in results if r.ok)
    for r in results:
        marker = "[PASS]" if r.ok else "[FAIL]"
        lines.append(f"  {marker} {r.label}")
        if r.detail:
            lines.append(f"      {r.detail}")
    lines.append("=" * 70)
    if pass_count == len(results):
        lines.append(f"RESULT: PASS ({pass_count}/{len(results)} criteria)")
    else:
        failed = [r.label for r in results if not r.ok]
        lines.append(
            f"RESULT: FAIL ({pass_count}/{len(results)} criteria, failing: {', '.join(failed)})"
        )
    lines.append("=" * 70)
    return "\n".join(lines)


def _render_json(results: Sequence[PhaseResult]) -> str:
    payload: dict[str, Any] = {
        "all_pass": all(r.ok for r in results),
        "phases": [{"label": r.label, "ok": r.ok, "detail": r.detail} for r in results],
    }
    return json.dumps(payload, indent=2)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the Phase 1 guardrail-enforcement orchestrator end-to-end "
            "against a freshly-migrated SQLite DB across four phases "
            "(composition primitive, orchestrator, repository provider, "
            "assembler integration)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to a freshly-migrated SQLite DB (overrides DATABASE_PATH and main.yaml).",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    return parser


async def _run_all_phases(db_path: Path) -> list[PhaseResult]:
    return [
        run_phase_1_composition_primitive(),
        run_phase_2_orchestrator(),
        await run_phase_3_repository_provider(db_path),
        await run_phase_4_assembler_integration(db_path),
    ]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on all-PASS, 1 if any phase FAILed."""
    configure_utf8_stdio()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = args.db_path
    if db_path is None:
        engine = make_engine(None)
        db_path = Path(str(engine.url.database or ""))
        engine.dispose()

    results = asyncio.run(_run_all_phases(db_path))

    if args.output == "json":
        print(_render_json(results))
    else:
        print(_render_text(results))

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
