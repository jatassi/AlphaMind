"""Smoke test: wipe_and_seed then bracket validation, no SDK calls.

Validates the synthetic seed produces brackets the codec can read back
into a valid :class:`BracketRecord` (the invariant that broke v4 with
``protective_legs must be non-empty``).
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
from alphamind.scheduler.debug_e2e.seed import wipe_and_seed
from alphamind.state.tables import BracketLegRow, BracketRow
from alphamind.state.tables.brackets_codec import rows_to_record

_DEBUG_DB = Path("data/alphamind-debug-e2e.db")


async def main() -> None:
    if not _DEBUG_DB.exists():  # noqa: ASYNC240 — diagnostic script; sync existence check at startup is fine
        raise SystemExit(f"missing {_DEBUG_DB}")
    work_db = _DEBUG_DB.with_name("alphamind-seed-smoke-debug-e2e.db")
    print(f"copy {_DEBUG_DB} -> {work_db}", flush=True)
    shutil.copyfile(_DEBUG_DB, work_db)

    now = datetime.now(UTC)

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{work_db}")
    async_session_factory = async_sessionmaker(async_engine, expire_on_commit=False)

    print("running wipe_and_seed ...", flush=True)
    async with async_session_factory() as session:
        await wipe_and_seed(
            session=session,
            now=now,
            db_path=str(work_db),
            portfolio=SYNTHETIC_PORTFOLIO,
        )
    print("seed OK", flush=True)

    async with async_session_factory() as session:
        bracket_rows = (await session.execute(select(BracketRow))).scalars().all()
        leg_rows = (await session.execute(select(BracketLegRow))).scalars().all()

    print(f"brackets={len(bracket_rows)}, legs={len(leg_rows)}", flush=True)
    if not bracket_rows:
        raise SystemExit("no brackets seeded")

    legs_by_bracket: dict[str, list[BracketLegRow]] = {}
    for leg in leg_rows:
        legs_by_bracket.setdefault(leg.bracket_id, []).append(leg)

    for bracket in bracket_rows:
        record = rows_to_record(bracket, tuple(legs_by_bracket.get(bracket.bracket_id, ())))
        print(
            f"  bracket {bracket.bracket_id}: status={record.status.value}, "
            f"legs={len(record.protective_legs)}",
            flush=True,
        )
    print("all brackets round-tripped through codec OK", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
