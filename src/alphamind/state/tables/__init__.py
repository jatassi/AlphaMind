"""SQLAlchemy table definitions for the state-persistence layer.

Importing this package registers all state-persistence tables on
``alphamind.persistence.models.Base.metadata`` so callers (tests using
``Base.metadata.create_all``, the Alembic env, downstream stories) see
them without having to remember each module name.
"""

from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.cash_ledger import CashLedgerRow
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.state.tables.drawdown_state import DrawdownStateRow
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.monitor_halt_mode import MonitorHaltModeRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.thesis_components import ThesisComponentRow

__all__ = [
    "ActivityLogRow",
    "BracketLegRow",
    "BracketRow",
    "CashLedgerRow",
    "CorporateActionIntegrationLedgerRow",
    "DrawdownStateRow",
    "FillRecordRow",
    "InvocationRow",
    "MonitorHaltModeRow",
    "OrderRow",
    "PositionRow",
    "ProcessLifetimeRow",
    "ThesisComponentRow",
    "ThesisRow",
]
