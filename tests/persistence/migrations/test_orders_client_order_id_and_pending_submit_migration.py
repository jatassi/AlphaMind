"""Tests for the migration that adds ``orders.client_order_id`` (unique-when-present)
and widens ``ck_orders_status`` with ``"PENDING_SUBMIT"`` (ALP-836).

The migration:

* adds the nullable ``client_order_id`` column + a unique index that admits many
  NULLs but one row per real key (the atomicity-first durable-intent key);
* widens the status CHECK so the new pre-dispatch ``PENDING_SUBMIT`` state can be
  persisted.

Like the positions-status-CANCELLED migration, we cannot prove "PENDING_SUBMIT
rejected at the previous revision" by upgrading forward — ``c6e3f4a5b8d9_add_orders``
builds ``ck_orders_status`` by iterating the *live* ``OrderStatus`` enum, so a
freshly-built DB already bakes the new member in. The existing production table
(created when the enum lacked it) carries the narrow constraint this migration
widens; our own downgrade reconstructs that narrow baseline to test against.

Orders rows carry a NOT NULL ``bracket_id`` FK into ``brackets`` (DEFERRABLE
INITIALLY DEFERRED). We never insert the parent rows; ``PRAGMA foreign_keys=OFF``
on the sync engine (where pysqlite's deferred BEGIN lets the PRAGMA run in
autocommit) lets the insert exercise the CHECK / unique index without standing up
the whole graph — the same dance ``tests/state/test_orders_table.py`` uses.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from alphamind.persistence.session import make_engine

_REVISION = "b4d9f1a7c2e6"
_PREVIOUS = "a7e3f1c9d2b4"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _insert_order(
    db_path: Path,
    *,
    order_id: str,
    status: str,
    client_order_id: str | None = None,
    include_client_order_id_column: bool = True,
) -> None:
    """Insert one minimal ``orders`` row with FK enforcement disabled.

    ``include_client_order_id_column=False`` omits the column entirely so the same
    helper works at the pre-migration (narrow) revision where the column does not
    yet exist.
    """
    cols = [
        "order_id",
        "position_id",
        "bracket_id",
        "order_role",
        "order_class",
        "instrument_spec_json",
        "direction",
        "order_type",
        "quantity",
        "price_parameters_json",
        "duration",
        "status",
        "alpaca_order_id",
        "alpaca_order_id_chain_json",
        "submission_timestamp",
        "last_update_timestamp",
        "filled_quantity",
        "remaining_quantity",
        "modification_count",
        "metadata_json",
    ]
    params: dict[str, object] = {
        "order_id": order_id,
        "bracket_id": "brk-test",
        "order_class": "SIMPLE",
        "spec_json": '{"instrument_type": "EQUITY", "ticker": "AAPL"}',
        "direction": "BUY",
        "status": status,
        "alp_id": f"alp-{order_id}",
        "chain_json": f'["alp-{order_id}"]',
        "ts": "2026-06-03T00:00:00+00:00",
    }
    values = [
        ":order_id",
        "NULL",
        ":bracket_id",
        "'ENTRY'",
        ":order_class",
        ":spec_json",
        ":direction",
        "'MARKET'",
        "1.0",
        '\'{"limit_price": null, "stop_trigger_price": null}\'',
        "'DAY'",
        ":status",
        ":alp_id",
        ":chain_json",
        ":ts",
        ":ts",
        "0.0",
        "1.0",
        "0",
        "'{\"age_hours\": 0.0}'",
    ]
    if include_client_order_id_column:
        cols.append("client_order_id")
        if client_order_id is None:
            values.append("NULL")
        else:
            values.append(":client_order_id")
            params["client_order_id"] = client_order_id

    sql = f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join(values)})"
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            # PRAGMA runs in pysqlite autocommit (deferred BEGIN), so foreign_keys
            # is genuinely disabled before the INSERT opens its transaction.
            conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            conn.execute(text(sql), params)
            conn.commit()
    finally:
        eng.dispose()


def _read_status(db_path: Path, order_id: str) -> str | None:
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            return conn.execute(
                text("SELECT status FROM orders WHERE order_id = :oid"),
                {"oid": order_id},
            ).scalar_one_or_none()
    finally:
        eng.dispose()


def _read_client_order_id(db_path: Path, order_id: str) -> str | None:
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            return conn.execute(
                text("SELECT client_order_id FROM orders WHERE order_id = :oid"),
                {"oid": order_id},
            ).scalar_one_or_none()
    finally:
        eng.dispose()


class TestOrdersClientOrderIdAndPendingSubmitMigration:
    def test_pending_submit_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """The new durable-intent status round-trips cleanly after upgrade."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _insert_order(db_path, order_id="ord-ps", status="PENDING_SUBMIT")
        assert _read_status(db_path, "ord-ps") == "PENDING_SUBMIT"

    def test_pre_existing_statuses_still_accepted_after_upgrade(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        for idx, status in enumerate(("PENDING", "PARTIALLY_FILLED", "FILLED", "CANCELLED")):
            _insert_order(db_path, order_id=f"ord-{idx}", status=status)
            assert _read_status(db_path, f"ord-{idx}") == status

    def test_client_order_id_column_round_trips(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _insert_order(
            db_path,
            order_id="ord-coid",
            status="PENDING_SUBMIT",
            client_order_id="inv-X.ENV-SA-1.0.0",
        )
        assert _read_client_order_id(db_path, "ord-coid") == "inv-X.ENV-SA-1.0.0"

    def test_client_order_id_unique_when_present(self, tmp_path: Path) -> None:
        """One row per real client_order_id; the second collides."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _insert_order(db_path, order_id="ord-a", status="PENDING", client_order_id="dup-coid")
        with pytest.raises(IntegrityError):
            _insert_order(db_path, order_id="ord-b", status="PENDING", client_order_id="dup-coid")

    def test_multiple_null_client_order_ids_allowed(self, tmp_path: Path) -> None:
        """Native-bracket protective children carry NULL — the unique index must
        admit many NULLs (SQLite treats them as distinct)."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _insert_order(db_path, order_id="ord-null-1", status="PENDING", client_order_id=None)
        _insert_order(db_path, order_id="ord-null-2", status="PENDING", client_order_id=None)
        assert _read_status(db_path, "ord-null-1") == "PENDING"
        assert _read_status(db_path, "ord-null-2") == "PENDING"

    def test_downgrade_narrows_then_upgrade_rewidens(self, tmp_path: Path) -> None:
        """Downgrade narrows the status vocabulary back (PENDING_SUBMIT rejected),
        re-upgrade admits it again — the genuine widen proof."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS)
        # Column is gone post-downgrade; omit it from the insert.
        with pytest.raises(IntegrityError):
            _insert_order(
                db_path,
                order_id="ord-ps-narrow",
                status="PENDING_SUBMIT",
                include_client_order_id_column=False,
            )
        command.upgrade(cfg, _REVISION)
        _insert_order(db_path, order_id="ord-ps-wide", status="PENDING_SUBMIT")
        assert _read_status(db_path, "ord-ps-wide") == "PENDING_SUBMIT"

    def test_column_and_unique_index_present_after_upgrade(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            col_names = {c["name"] for c in insp.get_columns("orders")}
            assert "client_order_id" in col_names
            idx = {i["name"]: i for i in insp.get_indexes("orders")}
            assert "ix_orders_client_order_id" in idx
            # SQLAlchemy's SQLite inspector reports ``unique`` as 1/0, not a bool.
            assert idx["ix_orders_client_order_id"]["unique"]
            assert idx["ix_orders_client_order_id"]["column_names"] == ["client_order_id"]
        finally:
            eng.dispose()

    def test_check_constraint_present_after_upgrade(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            checks = {c["name"]: c for c in inspect(eng).get_check_constraints("orders")}
            assert "ck_orders_status" in checks
            assert "PENDING_SUBMIT" in checks["ck_orders_status"]["sqltext"]
        finally:
            eng.dispose()

    def test_downgrade_refuses_when_pending_submit_rows_exist(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _insert_order(db_path, order_id="ord-ps", status="PENDING_SUBMIT")
        with pytest.raises(RuntimeError, match="PENDING_SUBMIT"):
            command.downgrade(cfg, _PREVIOUS)

    def test_up_down_up_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS)
        command.upgrade(cfg, _REVISION)
        _insert_order(db_path, order_id="ord-ps", status="PENDING_SUBMIT")
        assert _read_status(db_path, "ord-ps") == "PENDING_SUBMIT"
