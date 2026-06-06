"""SQLAlchemy table definitions for the state-persistence layer.

Importing this package registers all state-persistence tables on
``alphamind.persistence.models.Base.metadata`` so callers (tests using
``Base.metadata.create_all``, the Alembic env, downstream stories) see
them without having to remember each module name.
"""

from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.capital_reservations import CapitalReservationRow
from alphamind.state.tables.cash_ledger import CashLedgerRow
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.state.tables.counterfactual_replays import CounterfactualReplays
from alphamind.state.tables.drawdown_state import DrawdownStateRow
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.monitor_halt_mode import MonitorHaltModeRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.position_greeks import PositionGreeksRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow
from alphamind.state.tables.projection_rebuild_watermark import ProjectionRebuildWatermarkRow
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.thesis_components import ThesisComponentRow
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from alphamind.state.tables.unattributed_fills import UnattributedFillRow

__all__ = [
    "ActivityLogRow",
    "BracketLegRow",
    "BracketRow",
    "BrokerEventLogRow",
    "CapitalReservationRow",
    "CashLedgerRow",
    "CorporateActionIntegrationLedgerRow",
    "CounterfactualReplays",
    "DrawdownStateRow",
    "FillRecordRow",
    "InvocationRow",
    "MonitorHaltModeRow",
    "OrderRow",
    "PositionGreeksRow",
    "PositionRow",
    "ProcessLifetimeRow",
    "ProjectionRebuildWatermarkRow",
    "ThesisComponentRow",
    "ThesisPnlLedgerRow",
    "ThesisRow",
    "UnattributedFillRow",
]
