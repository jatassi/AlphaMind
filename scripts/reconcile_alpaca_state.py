"""Read-only broker-vs-Projection diff (prod ops inspection tool).

Compares the live Alpaca paper account — the System of Record (positions /
orders / cash) — against the local SQLite **Projection** of those Broker-Owned
Facts, and prints a divergence report. **Read-only, an inspection tool, never an
adjudicator** (ADR-0001, decision D): it never mutates Alpaca or the DB. A
mismatch is resolved by the in-pipeline projection rebuild (W2a /
``write_paths/projection_rebuild.py``), which folds the broker-event log onto the
live snapshot — this script only surfaces the diff for an operator, it does not
write Alpaca's value back.

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
    # Keyed by the local ``order_id`` PK (ALP-847): a monitor-enforced leg or a
    # not-yet-routed order carries ``alpaca_order_id IS NULL`` (the synthetic
    # ``alp-`` placeholder is deleted), so keying by the broker id would collapse
    # every such row onto a single NULL key. The broker id rides on each value.
    orders = {}
    for r in db.execute(
        "SELECT order_id, order_role, direction, status, quantity, filled_quantity, "
        "remaining_quantity, average_fill_price, alpaca_order_id FROM orders"
    ):
        m = _ORD_SYM.match(r["order_id"] or "")
        orders[r["order_id"]] = {
            "order_id": r["order_id"],
            "alpaca_order_id": r["alpaca_order_id"],
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
        # ``direction`` is a top-level positions column (LONG/SHORT/NULL), not a
        # details_json field — for EQUITY rows details_json carries no direction.
        "SELECT position_id, status, direction, "
        "json_extract(details_json,'$.ticker') t, "
        "json_extract(details_json,'$.share_count') sc FROM positions"
    ):
        positions[r["position_id"]] = {
            "sym": r["t"],
            "status": r["status"],
            "dir": r["direction"],
            "shares": r["sc"],
        }
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


def _signed_shares(p: dict[str, Any]) -> float:
    """Local share count signed by direction (SHORT negative) to match Alpaca's signed qty."""
    shares = float(p["shares"] or 0)
    return -shares if (p.get("dir") or "").upper() == "SHORT" else shares


def _leg_label(p: dict[str, Any]) -> str:
    """Per-leg local description, e.g. ``OPEN/SHORT:6.0sh`` (direction shown when present)."""
    direction = f"/{p['dir']}" if p.get("dir") else ""
    return f"{p['status']}{direction}:{p['shares']}sh"


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
        loc_desc = ", ".join(_leg_label(p) for p in locs) or "(none)"
        flag = ""
        # Compare signed-to-signed: a SHORT 6 nets to -6, matching Alpaca's -6.
        loc_open_qty = sum(_signed_shares(p) for p in locs if p["status"] != "CANCELLED")
        if abs(float(a_qty) - loc_open_qty) > 1e-9:
            flag = "  <<< DIVERGENCE"
        print(f"  {s:6} alpaca={a_qty} @ {a_px} uPL={a_pl} | local=[{loc_desc}]{flag}")


def _report_orders(aorders: dict[str, Any], local: dict[str, Any]) -> None:
    print("=" * 78)
    print("ORDERS  (local order -> alpaca status/fill)")
    for _oid, lo in sorted(local["orders"].items(), key=lambda kv: kv[1]["order_id"]):
        aid = lo["alpaca_order_id"]
        # ALP-847 — a NULL broker id is the expected steady state for a
        # monitor-enforced leg (armed Intent the continuous monitor enforces) or a
        # not-yet-routed order; it is NOT a divergence. The synthetic ``alp-``
        # placeholder is deleted, so there is no "synthetic, never dispatched" row.
        if aid is None:
            print(
                f"  {lo['sym']:5} {lo['role']:11} {lo['status']:9} local_filled={lo['filled']} "
                f"| NO BROKER ORDER (monitor-enforced leg / not-yet-routed)"
            )
            continue
        ao = aorders.get(aid)
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
    local_broker_ids = {lo["alpaca_order_id"] for lo in local["orders"].values()}
    orphans = [o for oid, o in aorders.items() if oid not in local_broker_ids]
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
