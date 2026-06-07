"""Read APIs against state-persistence tables.

Story 03 (ALP-357) ships the four ``activity_log`` query helpers. Story 06
(ALP-364) ships :class:`SqlPortfolioStateRepository` and the
``build_sql_portfolio_state_repository`` factory — the production-grade
implementation of :class:`PortfolioStateRepository`.

ALP-454 Pre-resolved decision (C): the Protocol surface is synchronous;
the provider callables passed into the factory are likewise sync.
"""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.repository.activity_log_queries import (
    read_intra_invocation_changelog,
    read_most_recent_config_change_new_hash,
    read_position_modification_trail,
    read_recent_pm_decision_log,
)
from alphamind.state.repository.agent_calls_queries import (
    insert_agent_call,
    read_agent_calls_for_agent,
    read_agent_calls_for_invocation,
    read_agent_calls_in_window,
)
from alphamind.state.repository.counterfactual_replays import (
    insert_counterfactual_replay,
    load_counterfactual_replays_for_envelope,
)
from alphamind.state.repository.digest_queries import (
    insert_weekly_digest_snapshot,
    read_weekly_digest_snapshot,
    read_weekly_digest_snapshots_in_range,
)
from alphamind.state.repository.position_state import (
    PositionStateNotFoundError,
    PositionStateSnapshot,
    load_position_state_at,
)
from alphamind.state.repository.sql_option_price_provider import (
    SqlOptionPriceProvider,
)
from alphamind.state.repository.sql_repository import (
    SqlPortfolioStateRepository,
)
from alphamind.state.repository.validation_queries import (
    derive_validation_status,
    insert_validation,
    insert_validation_outcome,
    mark_validation_superseded,
    read_outcomes_by_artifact,
    read_pending_validations,
    read_validation,
)


def build_sql_portfolio_state_repository(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    active_risk_parameters_provider: Callable[[], ActiveRiskParameterSet],
    prior_active_risk_parameters_provider: Callable[[str], ActiveRiskParameterSet],
    config: StatePersistenceConfig,
) -> PortfolioStateRepository:
    """Construct a production ``SqlPortfolioStateRepository`` conforming to the Protocol.

    The two ``*_active_risk_parameters_provider`` callables are the
    composition pipeline's seam for regime-adapted parameter resolution:
    the zero-arg current variant produces the live set; the path-keyed
    prior variant rebuilds the prior set from a stored
    ``resolved_config_snapshot_path``.
    """
    return SqlPortfolioStateRepository(
        session_factory=session_factory,
        invocation_id=invocation_id,
        active_risk_parameters_provider=active_risk_parameters_provider,
        prior_active_risk_parameters_provider=prior_active_risk_parameters_provider,
        config=config,
    )


__all__ = [
    "PositionStateNotFoundError",
    "PositionStateSnapshot",
    "SqlOptionPriceProvider",
    "SqlPortfolioStateRepository",
    "build_sql_portfolio_state_repository",
    "derive_validation_status",
    "insert_agent_call",
    "insert_counterfactual_replay",
    "insert_validation",
    "insert_validation_outcome",
    "insert_weekly_digest_snapshot",
    "load_counterfactual_replays_for_envelope",
    "load_position_state_at",
    "mark_validation_superseded",
    "read_agent_calls_for_agent",
    "read_agent_calls_for_invocation",
    "read_agent_calls_in_window",
    "read_intra_invocation_changelog",
    "read_most_recent_config_change_new_hash",
    "read_outcomes_by_artifact",
    "read_pending_validations",
    "read_position_modification_trail",
    "read_recent_pm_decision_log",
    "read_validation",
    "read_weekly_digest_snapshot",
    "read_weekly_digest_snapshots_in_range",
]
