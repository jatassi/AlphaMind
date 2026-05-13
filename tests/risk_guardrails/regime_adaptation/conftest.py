"""End-to-end test fixtures for the regime-adaptation work tree (story 10).

These fixtures power the multi-invocation lifecycle scenarios in
``test_e2e_regime_lifecycle.py``, ``test_e2e_overlay_overlap.py``, and
``test_e2e_emergency_passthrough.py``. Per the story file:

- ``in_memory_session`` — SQLite in-memory session with the schema applied.
- ``loaded_config_micro_normal`` — a real ``LoadedConfig`` built from the
  shipped ``config/`` tree.
- ``rule_metadata_from_shipped_registry`` — ``RuleMetadata`` map built from
  the active profile's ``rule_values`` keys (matches what the orchestrator
  consumes).
- ``held_position_at_5pct`` — a synthetic NVDA long position sized at 5%
  for breach-detector exercise paths.

The fixtures use the shipped configuration so the tests verify the
orchestrator against production config; a config edit (e.g., changing the
``position_max_size_pct`` crisis multiplier from 0.40) breaks the test,
which is intentional per the story file.

Note on profile selection: the story file names the config fixture
``loaded_config_micro_normal`` and suggests overriding ``active_profile``
to ``micro``. The shipped ``micro.yaml`` declares 11 ``rule_values``
keys; the shipped regime YAMLs declare 19 multiplier keys. The
orchestrator's parameter-set assembler requires the two key sets to match
(see ``parameter_set.py``'s assertion on
``set(interpolated_multipliers) == set(base_profile_rule_values)``), so
running the orchestrator against ``micro`` would fail. We instead use the
shipped ``medium`` profile, which declares the full 19 keys and matches
every regime's multiplier set. The shipped ``main.yaml`` already pins
``active_profile: medium``; the story's worked numeric values
(``position_max_size_pct: 5``, sector concentration 25, etc.) hold under
``medium`` identically.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind.config.loaders import (
    load_modes,
    load_overlays,
    load_profiles,
    load_regimes,
    load_run_types,
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
from alphamind.config.resolver import LoadedConfig
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.regime_adaptation import RuleMetadata

CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"


def _read_yaml(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


@pytest.fixture()
def in_memory_session() -> Iterator[Session]:
    """SQLite in-memory session with the full schema applied.

    Each test gets a fresh database; no cross-test state leakage.
    """
    engine: Engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as sess:
            yield sess
    finally:
        engine.dispose()


@pytest.fixture()
def loaded_config_micro_normal() -> LoadedConfig:
    """A ``LoadedConfig`` built from the shipped ``config/`` tree.

    Uses the shipped ``main.yaml`` (``active_profile=medium``) — see the
    module docstring for why we cannot use the ``micro`` profile here.
    Every other field is the shipped production value (regimes, overlays,
    scheduler, etc.).
    """
    return LoadedConfig(
        main=MainConfig.model_validate(_read_yaml("main.yaml")),
        scheduler=SchedulerConfig.model_validate(_read_yaml("scheduler.yaml")),
        venue=VenueConfig.model_validate(_read_yaml("venue.yaml")),
        execution=ExecutionConfig.model_validate(_read_yaml("execution.yaml")),
        guardrails=GuardrailsConfig.model_validate(_read_yaml("guardrails.yaml")),
        llm_failure=LLMFailureConfig.model_validate(_read_yaml("llm_failure.yaml")),
        digest=DigestConfig.model_validate(_read_yaml("digest.yaml")),
        assets=AssetsConfig.model_validate(_read_yaml("assets.yaml")),
        agents=AgentsConfig.model_validate(_read_yaml("agents.yaml")),
        continuous_monitor=ContinuousMonitorConfig.model_validate(
            _read_yaml("continuous_monitor.yaml")
        ),
        profiles=load_profiles(CONFIG_DIR),
        regimes=load_regimes(CONFIG_DIR),
        modes=load_modes(CONFIG_DIR),
        overlays=load_overlays(CONFIG_DIR),
        run_types=load_run_types(CONFIG_DIR),
    )


@pytest.fixture()
def rule_metadata_from_shipped_registry(
    loaded_config_micro_normal: LoadedConfig,
) -> Mapping[str, RuleMetadata]:
    """Build ``RuleMetadata`` for every rule the active profile declares.

    The orchestrator's parameter-set assembler requires metadata for every
    rule key in ``profile.rule_values``; we synthesize labels and units
    here since the shipped ``RuleRegistry`` is in a sibling work tree (the
    test's purpose is composition correctness, not registry integration).
    """
    profile = loaded_config_micro_normal.profiles[loaded_config_micro_normal.main.active_profile]
    return {
        rule_id: RuleMetadata(rule_id=rule_id, label=rule_id.replace("_", " "), unit="pct")
        for rule_id in profile.rule_values
    }


@pytest.fixture()
def held_position_at_5pct() -> PositionView:
    """A synthetic NVDA long position view sized at 5% of portfolio.

    Used by tests that verify the breach detector emits a
    ``RegimeTransitionBreach`` when a tightening transition (e.g., low_vol
    to crisis) drops the effective ``position_max_size_pct`` limit below
    the held weight (crisis 0.40 multiplier on micro's 5% base = 2%, so a
    5% position breaches by 3 percentage points).
    """
    fill_timestamp = datetime(2026, 4, 29, 13, 30, tzinfo=UTC)
    record = PositionRecord(
        position_id=PositionId("NVDA-LONG-1"),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=fill_timestamp,
        details=EquityPositionDetails(
            ticker=Symbol("NVDA"),
            share_count=10.0,
            average_cost_basis_per_share=100.0,
            borrow_rate_pct=None,
            locate_status=None,
            margin_held_usd=None,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=fill_timestamp,
                fill_price=100.0,
                fill_quantity=10.0,
                slippage=0.01,
                fees=0.5,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=1000.0,
        unrealized_pnl_usd=0.0,
        unrealized_pnl_pct=0.0,
        position_weight_pct=5.0,
        position_age_hours=0.0,
        notional_exposure_usd=1000.0,
        delta_adjusted_exposure_usd=1000.0,
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )
