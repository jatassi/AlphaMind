"""SQLAlchemy table definitions for the state-persistence layer.

Importing this package registers all state-persistence tables on
``alphamind.persistence.models.Base.metadata`` so callers (tests using
``Base.metadata.create_all``, the Alembic env, downstream stories) see
them without having to remember each module name.
"""

from alphamind.execution.state_persistence.tables.activity_log import ActivityLogRow
from alphamind.execution.state_persistence.tables.bracket_legs import BracketLegRow
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.cash_ledger import CashLedgerRow
from alphamind.execution.state_persistence.tables.drawdown_state import DrawdownStateRow
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.process_lifetimes import ProcessLifetimeRow
from alphamind.execution.state_persistence.tables.theses import ThesisRow
from alphamind.execution.state_persistence.tables.thesis_components import ThesisComponentRow

__all__ = [
    "ActivityLogRow",
    "BracketLegRow",
    "BracketRow",
    "CashLedgerRow",
    "DrawdownStateRow",
    "InvocationRow",
    "PositionRow",
    "ProcessLifetimeRow",
    "ThesisComponentRow",
    "ThesisRow",
]
