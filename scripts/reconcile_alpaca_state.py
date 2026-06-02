"""Read-only Alpaca <-> local-state reconciliation (prod ops investigation).

Compares the live Alpaca paper account (positions / orders / cash) against the
local SQLite portfolio state, and prints a divergence report. Read-only: it
never mutates Alpaca or the DB.

Run from the repo root:
    set -a && source <(tr -d '\r' < .env) && set +a && \
        uv run python scripts/reconcile_alpaca_state.py
(or rely on load_dotenv below if .env is well-formed)
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from decimal import Decimal
from typing import Any

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from alphamind.execution.broker_adapter.queries import AccountStateQueries

load_dotenv()

DB_PATH = os.path.expandvars(r"%USERPROFILE%\AlphaMind\data\alphamind.db")
_ORD_SYM = re.compile(r"^ORD-([A-Z.]+)-")


def _key(s: str) -> str:
    return (os.environ.get(s) or "").strip()


def build_client() -> TradingClient:
    api_key = _key("ALPACA_PAPER_KEY") or _key("ALPACA_API_KEY")
    secret = _key("ALPACA_PAPER_SECRET") or _key("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        raise SystemExit("ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET not set in env/.env")
    return TradingClient(api_key=api_key, secret_key=secret, paper=True)


def load_local() -> dict[str, Any]:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    orders = {}
    for r in db.execute(
        "SELECT order_id, order_role, direction, status, quantity, filled_quantity, "
        "remaining_quantity, average_fill_price, alpaca_order_id FROM orders"
    ):
        m = _ORD_SYM.match(r["order_id"] or "")
        orders[r["alpaca_order_id"]] = {
            "order_id": r["order_id"],
            "sym": m.group(1) if m else "?",
            "role": r["order_role"],
            "dir": r["direction"],
            "status": r["status"],
            "qty": r["quantity"],
            "filled": r["filled_quantity"],
            "remaining": r["remaining_quantity"],
            "avgpx": r["average_fill_price"],
        }
    positions = {}
    for r in db.execute(
        "SELECT position_id, status, "
        "json_extract(details_json,'$.ticker') t, "
        "json_extract(details_json,'$.share_count') sc FROM positions"
    ):
        positions[r["position_id"]] = {"sym": r["t"], "status": r["status"], "shares": r["sc"]}
    cash = dict(db.execute("SELECT * FROM cash_ledger").fetchone())
    db.close()
    return {"orders": orders, "positions": positions, "cash": cash}


def _report_account(acct: Any, local: dict[str, Any]) -> None:
    print("=" * 78)
    print("ALPACA ACCOUNT:", acct.status)
    print(f"  cash={acct.cash}  equity={acct.equity}  buying_power={acct.buying_power}")
    c = local["cash"]
    print("LOCAL cash_ledger:")
    print(
        f"  current_cash={c['current_cash_usd']}  settled={c['settled_cash_usd']}  "
        f"reserved={c['reserved_capital_usd']}  bp={c['available_buying_power_usd']}"
    )
    drift = Decimal(str(acct.cash)) - Decimal(str(c["current_cash_usd"]))
    print(f"  >> CASH DRIFT (alpaca - local current_cash) = {drift}")


def _report_positions(apos: dict[str, Any], local: dict[str, Any]) -> None:
    print("=" * 78)
    print("POSITIONS  (alpaca qty | local shares | status)")
    syms = sorted(set(apos) | {p["sym"] for p in local["positions"].values() if p["sym"]})
    for s in syms:
        a = apos.get(s)
        locs = [p for p in local["positions"].values() if p["sym"] == s]
        a_qty = a.qty if a else 0
        a_px = a.avg_entry_price if a else "-"
        a_pl = a.unrealized_pl if a else "-"
        loc_desc = ", ".join(f"{p['status']}:{p['shares']}sh" for p in locs) or "(none)"
        flag = ""
        loc_open_sh = sum(float(p["shares"] or 0) for p in locs if p["status"] != "CANCELLED")
        if abs(float(a_qty) - loc_open_sh) > 1e-9:
            flag = "  <<< DIVERGENCE"
        print(f"  {s:6} alpaca={a_qty} @ {a_px} uPL={a_pl} | local=[{loc_desc}]{flag}")


def _report_orders(aorders: dict[str, Any], local: dict[str, Any]) -> None:
    print("=" * 78)
    print("ORDERS  (local order -> alpaca status/fill)")
    for aid, lo in sorted(local["orders"].items(), key=lambda kv: kv[1]["order_id"]):
        synthetic = aid.startswith("alp-")
        ao = aorders.get(aid)
        if synthetic:
            seen = "NOT-ON-ALPACA (synthetic, never dispatched)"
            print(
                f"  {lo['sym']:5} {lo['role']:11} {lo['status']:9} local_filled={lo['filled']} "
                f"| {seen}  <<< SYNTHETIC"
            )
            continue
        if ao is None:
            print(
                f"  {lo['sym']:5} {lo['role']:11} {lo['status']:9} local_filled={lo['filled']} "
                f"| alpaca_id={aid[:8]} NOT FOUND on Alpaca  <<< MISSING-ON-BROKER"
            )
            continue
        mism = ""
        if abs(float(lo["filled"] or 0) - float(ao.filled_qty or 0)) > 1e-9:
            mism = "  <<< FILL-QTY MISMATCH"
        elif lo["status"] == "CANCELLED" and ao.status not in ("canceled", "cancelled", "expired"):
            mism = f"  <<< STATUS MISMATCH (alpaca={ao.status})"
        print(
            f"  {lo['sym']:5} {lo['role']:11} {lo['status']:9} local_filled={lo['filled']} "
            f"| alpaca={ao.status} filled={ao.filled_qty}@{ao.filled_avg_price}{mism}"
        )

    # Alpaca orders with no local record (broker-side orphans), recent first
    local_ids = set(local["orders"])
    orphans = [o for oid, o in aorders.items() if oid not in local_ids]
    if orphans:
        print("-" * 78)
        print(f"ALPACA orders with NO local record ({len(orphans)}):")
        for o in sorted(orphans, key=lambda o: o.submitted_at, reverse=True)[:20]:
            print(
                f"  {o.symbol:6} {o.side:5} {o.order_class:9} {o.status:11} "
                f"qty={o.qty} filled={o.filled_qty} submitted={o.submitted_at.isoformat()}"
            )
    print("=" * 78)


async def main() -> None:
    q = AccountStateQueries(build_client())
    acct = q.get_account()
    apos = {p.symbol: p for p in q.get_positions()}
    aorders = {}
    async for o in q.get_orders(status="all"):
        aorders[o.order_id] = o
    local = load_local()

    _report_account(acct, local)
    _report_positions(apos, local)
    _report_orders(aorders, local)


if __name__ == "__main__":
    asyncio.run(main())
