"""Standalone reproduction of the energy_researcher SDK hang.

Invokes ``invoke_domain_researcher`` ONCE against the captured
user_message from the most recent failed debug-e2e run. No parallelism,
no TaskGroup, no other agents running. Confirms whether the hang is
energy-specific (deterministic content/config issue) vs concurrency-
related (would clear up when run alone).

Usage:

    set -a && source .env && set +a && \\
        uv run python scripts/_debug_energy_isolated.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.harness import invoke_domain_researcher
from alphamind.config.load import parse_loaded_config
from alphamind.config.models.agents import AgentName

_ARCHIVE_ROOT = Path(".archive/verify-debug-e2e/invocations")
_REPRO_ARCHIVE = Path(".archive/repro-energy-isolated")


def _latest_energy_user_message() -> str:
    candidates = sorted(
        _ARCHIVE_ROOT.glob("inv-*/analysis/energy_researcher/user_message.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(
            f"No captured energy_researcher user_message.md under {_ARCHIVE_ROOT} — "
            "run debug-e2e verify at least once to produce one."
        )
    print(f"using user_message from: {candidates[0]}")
    return candidates[0].read_text(encoding="utf-8")


async def main() -> None:
    loaded = parse_loaded_config(Path("config"))
    agent_config = loaded.agents.agents[AgentName.energy_researcher]
    user_message = _latest_energy_user_message()
    print(f"user_message size: {len(user_message)} bytes")
    print(f"agent model: {agent_config.model}")
    print(f"latency_budget_seconds: {agent_config.latency_budget_seconds}")

    invocation_id = f"repro-{int(time.time())}"

    print(f"invoking energy_researcher solo (invocation_id={invocation_id}) ...")
    wall_start = time.monotonic()
    try:
        result = await invoke_domain_researcher(
            agent_config=agent_config,
            sector=Sector.ENERGY,
            user_message=user_message,
            invocation_id=invocation_id,
            archive_root=_REPRO_ARCHIVE,
        )
    except Exception as exc:
        elapsed = time.monotonic() - wall_start
        print(f"FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        raise
    else:
        elapsed = time.monotonic() - wall_start
        print(
            f"OK after {elapsed:.1f}s: retry_count={result.retry_count}, "
            f"tokens={result.tokens_used}"
        )


if __name__ == "__main__":
    _REPRO_ARCHIVE.mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
