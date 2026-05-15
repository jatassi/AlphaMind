"""Round-trip codec between :class:`CashLedger` and :class:`CashLedgerRow`.

Persisted subset of the typed record: ``current_cash_usd``,
``settled_cash_usd``, ``reserved_capital_usd``, ``available_buying_power_usd``,
``margin_held_usd``, ``unsettled_proceeds``. Read-time computed fields
(``cash_pct_of_portfolio``, ``true_deployable_capital_usd``, ``regt_excess_*``)
must be supplied by the caller when rehydrating a typed record from a row;
the storage layer never invents them.
"""

from __future__ import annotations

import json
from datetime import datetime

from alphamind.portfolio_state.records import CashLedger, UnsettledProceedsEntry
from alphamind.state.tables._singleton_codec import (
    datetime_to_iso_z,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)


def _serialize_unsettled_proceeds(proceeds: tuple[UnsettledProceedsEntry, ...]) -> str:
    """Encode an unsettled-proceeds tuple as a JSON array.

    Hand-rolled JSON; mirrors :class:`UnsettledProceedsEntry`'s frozen-dataclass
    field set. Any future field additions require a matching codec update.
    """
    return json.dumps(
        [
            {
                "settlement_date": entry.settlement_date.isoformat(),
                "amount_usd": entry.amount_usd,
                "source_transaction_id": entry.source_transaction_id,
            }
            for entry in proceeds
        ]
    )


def _deserialize_unsettled_proceeds(payload: str) -> tuple[UnsettledProceedsEntry, ...]:
    raw = json.loads(payload)
    return tuple(
        UnsettledProceedsEntry(
            settlement_date=datetime.fromisoformat(item["settlement_date"]),
            amount_usd=item["amount_usd"],
            source_transaction_id=item["source_transaction_id"],
        )
        for item in raw
    )


def cash_ledger_record_to_row(
    record: CashLedger,
    *,
    last_updated_at: datetime,
) -> CashLedgerRow:
    """Build a ``CashLedgerRow`` from a typed ``CashLedger``.

    ``last_updated_at`` is storage-time metadata supplied by the write
    path; the typed record itself does not carry it.
    """
    return CashLedgerRow(
        id=CASH_LEDGER_SINGLETON_ID,
        current_cash_usd=record.current_cash_usd,
        settled_cash_usd=record.settled_cash_usd,
        reserved_capital_usd=record.reserved_capital_usd,
        available_buying_power_usd=record.available_buying_power_usd,
        margin_held_usd=record.margin_held_usd,
        unsettled_proceeds_json=_serialize_unsettled_proceeds(record.unsettled_proceeds),
        last_updated_at=datetime_to_iso_z(last_updated_at, field_name="last_updated_at"),
    )


def cash_ledger_record_from_row(
    row: CashLedgerRow,
    *,
    cash_pct_of_portfolio: float,
    true_deployable_capital_usd: float,
    regt_excess_trailing_30d_usd: float,
    regt_excess_trailing_90d_usd: float,
    regt_excess_lifetime_usd: float,
) -> CashLedger:
    """Rehydrate a typed ``CashLedger`` from a row plus computed kwargs.

    The five computed fields are required because the storage layer
    persists only the running-state subset of the typed record; the
    derived values (cash-percent, true deployable, RegT-excess history)
    must be recomputed from current portfolio context.
    """
    # ALP-462 — the DecimalText columns hand back ``Decimal`` values, but the
    # typed ``CashLedger`` record (in portfolio_state.records.cash) is out of
    # ALP-462's file list and still carries ``float`` fields. Cast at the
    # codec boundary so the dataclass receives the expected types; without the
    # cast the dataclass would carry ``Decimal`` values silently.
    return CashLedger(
        current_cash_usd=float(row.current_cash_usd),
        settled_cash_usd=float(row.settled_cash_usd),
        reserved_capital_usd=float(row.reserved_capital_usd),
        available_buying_power_usd=float(row.available_buying_power_usd),
        margin_held_usd=float(row.margin_held_usd),
        unsettled_proceeds=_deserialize_unsettled_proceeds(row.unsettled_proceeds_json),
        cash_pct_of_portfolio=cash_pct_of_portfolio,
        true_deployable_capital_usd=true_deployable_capital_usd,
        regt_excess_trailing_30d_usd=regt_excess_trailing_30d_usd,
        regt_excess_trailing_90d_usd=regt_excess_trailing_90d_usd,
        regt_excess_lifetime_usd=regt_excess_lifetime_usd,
    )
