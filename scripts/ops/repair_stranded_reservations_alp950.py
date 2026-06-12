"""One-time ALP-950 repair: release reserved capital stranded by pre-ALP-944 fills.

Two short-entry reservations (BAC 1,977.480 / TXN 2,583.90, both reserved
2026-06-10) were never released because the pre-ALP-944 fill-integration code
keyed the release off the buy side instead of the entry role — short entries
strand. Both positions are now CLOSED, so the stakes sit in
``cash_ledger.reserved_capital_usd`` forever and the PM's "Available for new
positions" guardrail header understates sizing headroom by ~4.5% every
invocation. ALP-944 (deployed ``ac2e6dc7``) fixes the lifecycle forward-only;
this script is the sanctioned one-time retroactive release.

What it does (``--execute``; default is a read-only dry run):

1. Reconciles every CAPITAL_RESERVED row against CAPITAL_RELEASED rows by
   order_id and classifies the outstanding remainder by position status —
   OPEN/PENDING outstanding is legitimate, CLOSED outstanding is stranded.
2. Refuses to act unless the derived stranded set matches the ALP-950 pinned
   set exactly (fail-closed; a second run finds nothing stranded and exits).
3. Mints real ``process_lifetimes`` + ``invocations`` rows (operator-console
   pattern — ``trigger_source='operator_console'``, ``trigger_reason``
   carrying the ALP-950 rationale) so the audit rows FK into an invocation
   that explains the ledger move.
4. Emits one typed CAPITAL_RELEASED activity row per stranded order
   (``source=OPERATOR_CONSOLE``, entry_id token ``alp950repair``) and
   decrements ``reserved_capital_usd`` in the same transaction, mirroring the
   command-execution ``_release_capital`` primitive (zero floor).
5. Re-runs the reconciliation and asserts the ALP-950 acceptance criteria:
   every reservation is released or backed by an OPEN/PENDING position, and
   the ledger equals the legitimate outstanding sum.

Run from the repo root on the production machine (no vendor APIs — no .env
needed)::

    uv run python scripts/ops/repair_stranded_reservations_alp950.py            # dry run
    uv run python scripts/ops/repair_stranded_reservations_alp950.py --execute  # repair
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import socket
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.money import money
from alphamind.persistence.session import engine_pair_context
from alphamind.portfolio_state.events.activity_log import (
    CapitalReleasedDetail,
    CapitalReservedDetail,
    EventSource,
    EventType,
    decode_detail,
)
from alphamind.state.invocation_context.activity_log import emit_activity_log_entry
from alphamind.state.invocation_context.context import InvocationContext, InvocationHandle
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.cash_ledger import CASH_LEDGER_SINGLETON_ID, CashLedgerRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.positions import PositionRow

# The ALP-950 stranded set, pinned. Execution refuses unless the live-derived
# stranded set matches this exactly — the script can never release anything
# else, and a re-run (nothing stranded anymore) exits without writing.
EXPECTED_STRANDED: dict[str, Decimal] = {
    "ORD-BAC-entry-758df0ce1de15e1992180dd8be157ecf": Decimal("1977.480"),
    "ORD-TXN-entry-0c48588c5ca7502eaf78cfef7bff7c1f": Decimal("2583.90"),
}

# Self-identifying entry_id tokens (custom-token caller per
# ``build_activity_log_entry``) so the repair rows are greppable by PK.
_ENTRY_TOKEN_BY_ORDER: dict[str, str] = {
    "ORD-BAC-entry-758df0ce1de15e1992180dd8be157ecf": "alp950repair-bac",
    "ORD-TXN-entry-0c48588c5ca7502eaf78cfef7bff7c1f": "alp950repair-txn",
}

_TRIGGER_REASON = (
    "verb=repair_stranded_reservations_alp950 ALP-950: retroactive release of "
    "pre-ALP-944 short-entry reservations stranded on CLOSED positions "
    "(BAC 1977.480 + TXN 2583.90 = 4561.380)"
)

# Sentinel for the pipeline-only invocation columns, mirroring the command
# center's ``operator_invocation`` convention ("not applicable" filler).
_SENTINEL = "operator_action"
_EMPTY_JSON = "{}"

# Position statuses that legitimately hold an outstanding reservation.
_LIVE_POSITION_STATUSES = frozenset({"OPEN", "PENDING"})

# An invocation younger than this with no command-execution stamp is treated
# as in flight; the repair refuses to interleave with a running pipeline.
_INFLIGHT_WINDOW = timedelta(hours=2)


@dataclass(frozen=True)
class OrderReservation:
    """Net reservation state for one entry order, folded from activity_log."""

    order_id: str
    position_id: str | None
    thesis_id: str | None
    reserved_usd: Decimal
    released_usd: Decimal
    position_status: str | None

    @property
    def outstanding_usd(self) -> Decimal:
        return self.reserved_usd - self.released_usd


@dataclass(frozen=True)
class Reconciliation:
    """Full reservation reconciliation + the ledger value it must match."""

    orders: tuple[OrderReservation, ...]
    ledger_reserved_usd: Decimal

    @property
    def outstanding(self) -> tuple[OrderReservation, ...]:
        return tuple(o for o in self.orders if o.outstanding_usd > 0)

    @property
    def stranded(self) -> tuple[OrderReservation, ...]:
        return tuple(
            o for o in self.outstanding if o.position_status not in _LIVE_POSITION_STATUSES
        )

    @property
    def legitimate(self) -> tuple[OrderReservation, ...]:
        return tuple(o for o in self.outstanding if o.position_status in _LIVE_POSITION_STATUSES)


async def reconcile(session: AsyncSession) -> Reconciliation:
    """Fold CAPITAL_RESERVED / CAPITAL_RELEASED rows into per-order net state."""
    stmt = (
        select(ActivityLogRow)
        .where(
            ActivityLogRow.event_type.in_(
                (EventType.CAPITAL_RESERVED.value, EventType.CAPITAL_RELEASED.value)
            )
        )
        .order_by(ActivityLogRow.entry_at)
    )
    reserved: dict[str, Decimal] = {}
    released: dict[str, Decimal] = {}
    position_by_order: dict[str, str | None] = {}
    thesis_by_order: dict[str, str | None] = {}
    for row in (await session.execute(stmt)).scalars():
        if row.event_type == EventType.CAPITAL_RESERVED.value:
            detail_reserved = decode_detail(row.detail_json, CapitalReservedDetail)
            order_id = row.order_id or detail_reserved.order_id
            amount = Decimal(detail_reserved.amount_usd)
            reserved[order_id] = reserved.get(order_id, Decimal(0)) + amount
            position_by_order.setdefault(order_id, row.position_id)
            thesis_by_order.setdefault(order_id, row.thesis_id)
        else:
            detail_released = decode_detail(row.detail_json, CapitalReleasedDetail)
            order_id = row.order_id or detail_released.order_id
            released[order_id] = released.get(order_id, Decimal(0)) + Decimal(
                detail_released.amount_usd
            )

    position_ids = sorted({p for p in position_by_order.values() if p is not None})
    status_by_position: dict[str, str] = {}
    if position_ids:
        pos_stmt = select(PositionRow.position_id, PositionRow.status).where(
            PositionRow.position_id.in_(position_ids)
        )
        for position_id, status in (await session.execute(pos_stmt)).all():
            status_by_position[position_id] = status

    orders = tuple(
        OrderReservation(
            order_id=order_id,
            position_id=position_by_order.get(order_id),
            thesis_id=thesis_by_order.get(order_id),
            reserved_usd=total,
            released_usd=released.get(order_id, Decimal(0)),
            position_status=status_by_position.get(position_by_order.get(order_id) or ""),
        )
        for order_id, total in sorted(reserved.items())
    )

    cash_row = await session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if cash_row is None:
        msg = "cash_ledger singleton missing — cannot reconcile"
        raise SystemExit(msg)
    return Reconciliation(orders=orders, ledger_reserved_usd=Decimal(cash_row.reserved_capital_usd))


def report(recon: Reconciliation) -> None:
    """Print the reconciliation table and the derived repair plan."""
    print("=" * 78)
    print("RESERVATION RECONCILIATION (activity_log CAPITAL_RESERVED vs CAPITAL_RELEASED)")
    for o in recon.orders:
        if o.outstanding_usd == 0:
            verdict = "matched"
        elif o.position_status in _LIVE_POSITION_STATUSES:
            verdict = f"OUTSTANDING (legitimate — position {o.position_status})"
        else:
            verdict = f"STRANDED (position {o.position_status})"
        print(
            f"  {o.order_id:55} reserved={o.reserved_usd:>9} "
            f"released={o.released_usd:>9}  {verdict}"
        )
    outstanding_total = sum((o.outstanding_usd for o in recon.outstanding), Decimal(0))
    stranded_total = sum((o.outstanding_usd for o in recon.stranded), Decimal(0))
    print("-" * 78)
    print(f"  ledger reserved_capital_usd = {recon.ledger_reserved_usd}")
    print(f"  outstanding total           = {outstanding_total}")
    print(f"  stranded (repair) total     = {stranded_total}")
    print(f"  post-repair ledger expected = {recon.ledger_reserved_usd - stranded_total}")
    print("=" * 78)


def gate_problems(recon: Reconciliation) -> list[str]:
    """Fail-closed checks that must all pass before the repair may write."""
    problems: list[str] = []
    outstanding_total = sum((o.outstanding_usd for o in recon.outstanding), Decimal(0))
    if outstanding_total != recon.ledger_reserved_usd:
        problems.append(
            f"ledger reserved_capital_usd={recon.ledger_reserved_usd} does not equal the "
            f"activity_log outstanding total {outstanding_total} — reconcile manually first"
        )
    derived = {o.order_id: o.outstanding_usd for o in recon.stranded}
    if {k: +v for k, v in derived.items()} != {k: +v for k, v in EXPECTED_STRANDED.items()}:
        problems.append(
            f"derived stranded set {derived} does not match the ALP-950 pinned set "
            f"{EXPECTED_STRANDED} — refusing (nothing stranded means the repair already ran)"
        )
    for o in recon.stranded:
        if o.position_id is None:
            problems.append(f"stranded order {o.order_id} has no position_id — refusing")
    return problems


async def newest_inflight_invocation(session: AsyncSession) -> str | None:
    """Return the newest non-operator invocation that looks mid-run, if any."""
    stmt = select(InvocationRow).order_by(InvocationRow.start_at.desc()).limit(10)
    now = datetime.now(UTC)
    for row in (await session.execute(stmt)).scalars():
        if row.trigger_source == "operator_console":
            continue
        if row.command_execution_completed_at is not None:
            return None
        started = datetime.fromisoformat(row.start_at)
        if now - started < _INFLIGHT_WINDOW:
            inflight_id: str = row.invocation_id
            return inflight_id
        return None  # an old NULL stamp is an abort, not an in-flight run
    return None


def _git(*args: str) -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    ).stdout.strip()


def _provenance() -> tuple[str, str, bool]:
    """Best-effort (sha, branch, dirty) for the process-lifetime row."""
    try:
        return (
            _git("rev-parse", "HEAD"),
            _git("rev-parse", "--abbrev-ref", "HEAD"),
            bool(_git("status", "--porcelain")),
        )
    except (subprocess.CalledProcessError, OSError):
        return "unknown", "unknown", False


def _iso_z(stamp: datetime) -> str:
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_rows(now: datetime) -> tuple[ProcessLifetimeRecord, InvocationRecord]:
    """Mint the process-lifetime + invocation rows the audit entries FK into."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    sha, branch, dirty = _provenance()
    lifetime = ProcessLifetimeRecord(
        process_lifetime_id=f"plt-opsrepair-{stamp}-{uuid.uuid4().hex[:8]}",
        process_role="pipeline",
        process_start_at=_iso_z(now),
        process_pid=os.getpid(),
        hostname=socket.gethostname(),
        git_sha=sha,
        git_branch=branch,
        git_dirty=dirty,
        python_version=platform.python_version(),
        pip_freeze_hash=_SENTINEL,
        pip_freeze_snapshot_path=_SENTINEL,
        anthropic_sdk_version=_SENTINEL,
        claude_agent_sdk_version=_SENTINEL,
        os_release=platform.platform(),
    )
    invocation = InvocationRecord(
        invocation_id=f"inv-{stamp}-{uuid.uuid4().hex[:8]}",
        process_lifetime_id=lifetime.process_lifetime_id,
        start_at=_iso_z(now),
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="manual",
        trigger_source="operator_console",
        trigger_reason=_TRIGGER_REASON,
        git_sha_at_invocation=sha,
        active_profile=_SENTINEL,
        active_regime=_SENTINEL,
        active_mode="normal",
        active_overlays_json=_EMPTY_JSON,
        resolved_config_hash=_SENTINEL,
        resolved_config_snapshot_path=_SENTINEL,
        feature_flags_snapshot_json=_EMPTY_JSON,
        data_calibration_state_snapshot_path=_SENTINEL,
        data_source_freshness_json=_EMPTY_JSON,
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )
    return lifetime, invocation


async def _release_stranded(handle: InvocationHandle, now: datetime) -> None:
    """Re-derive inside the write transaction, then release + emit per order."""
    recon = await reconcile(handle.session)
    problems = gate_problems(recon)
    if problems:
        msg = "gates failed inside the write transaction:\n  " + "\n  ".join(problems)
        raise SystemExit(msg)

    cash_row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if cash_row is None:
        msg = "cash_ledger singleton missing — cannot repair"
        raise SystemExit(msg)
    for order in recon.stranded:
        amount = money(order.outstanding_usd)
        net = cash_row.reserved_capital_usd - amount
        if net < 0:
            # Mirrors _release_capital's zero floor (ALP-741): never poison the
            # singleton, but a clamp here means the gates above missed something.
            print(f"WARNING: release of {amount} for {order.order_id} floored at zero ({net})")
        cash_row.reserved_capital_usd = money(max(net, Decimal(0)))
        cash_row.last_updated_at = now.isoformat()
        emit_activity_log_entry(
            handle,
            event_type=EventType.CAPITAL_RELEASED,
            position_id=order.position_id,
            order_id=order.order_id,
            thesis_id=order.thesis_id,
            timestamp=now,
            detail=CapitalReleasedDetail(order_id=order.order_id, amount_usd=amount),
            source=EventSource.OPERATOR_CONSOLE,
            entry_id=(
                f"{handle.invocation_id}-CAPITAL_RELEASED-{_ENTRY_TOKEN_BY_ORDER[order.order_id]}"
            ),
        )
        print(f"released {amount} for {order.order_id} (position {order.position_id})")


async def execute_repair(factory: async_sessionmaker[AsyncSession]) -> None:
    """Insert the audit scaffolding rows, then run the repair transaction."""
    now = datetime.now(UTC)
    lifetime, invocation = _build_rows(now)
    async with factory() as session:
        session.add(process_lifetime_record_to_row(lifetime))
        await session.commit()
    async with InvocationContext(session_factory=factory, record=invocation) as handle:
        await _release_stranded(handle, now)
    print(f"repair committed under invocation {invocation.invocation_id}")


async def verify(session: AsyncSession) -> bool:
    """ALP-950 acceptance criteria — True when the post-repair state holds."""
    recon = await reconcile(session)
    report(recon)
    failures: list[str] = []
    if recon.stranded:
        failures.append(f"stranded reservations remain: {[o.order_id for o in recon.stranded]}")
    legit_total = sum((o.outstanding_usd for o in recon.legitimate), Decimal(0))
    if recon.ledger_reserved_usd != legit_total:
        failures.append(
            f"ledger reserved_capital_usd={recon.ledger_reserved_usd} != "
            f"open/pending outstanding total {legit_total}"
        )
    for failure in failures:
        print(f"VERIFY FAIL: {failure}")
    if not failures:
        print("VERIFY PASS: ledger equals open/pending outstanding; nothing stranded.")
    return not failures


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="apply the repair (default: dry run)"
    )
    parser.add_argument("--db", default=None, help="explicit DB path (default: configured path)")
    parser.add_argument(
        "--allow-inflight",
        action="store_true",
        help="proceed even when the newest invocation looks mid-run",
    )
    args = parser.parse_args()

    async with engine_pair_context(args.db) as engines:
        factory = engines.async_session_factory
        async with factory() as session:
            recon = await reconcile(session)
            report(recon)
            problems = gate_problems(recon)
            inflight = await newest_inflight_invocation(session)
        if inflight is not None and not args.allow_inflight:
            problems.append(
                f"invocation {inflight} looks in flight — wait for it to finish "
                "(or pass --allow-inflight)"
            )
        for problem in problems:
            print(f"GATE: {problem}")
        if not args.execute:
            print("DRY RUN — no changes written." + (" Gates would PASS." if not problems else ""))
            return 1 if problems else 0
        if problems:
            print("refusing to execute.")
            return 1

        await execute_repair(factory)
        async with factory() as session:
            ok = await verify(session)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
