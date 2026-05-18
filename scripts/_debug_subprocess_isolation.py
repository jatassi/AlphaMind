"""Smoke test for the subprocess-isolated domain researcher harness.

Invokes ``invoke_domain_researcher_in_subprocess`` (the workaround for
the SDK process-state stall) TWICE in sequence in the same Python
process. Each call spawns a fresh worker subprocess, so each SDK call
is effectively the "first call" in its own process — neither call
should hit the stall pattern that the in-process back-to-back
reproduction exhibited.

Usage:

    set -a && source .env && set +a && \\
        uv run python scripts/_debug_subprocess_isolation.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from alphamind.analysis._sdk_subprocess import invoke_domain_researcher_in_subprocess
from alphamind.analysis._shared import Sector
from alphamind.config.load import parse_loaded_config
from alphamind.config.models.agents import AgentName

_ARCHIVE_ROOT = Path(".archive/verify-debug-e2e/invocations")
_REPRO_ARCHIVE = Path(".archive/repro-subprocess-isolation")


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
    print(f"using user_message from: {candidates[0]}", flush=True)
    return candidates[0].read_text(encoding="utf-8")


async def _invoke(label: str, user_message: str, agent_config: object) -> None:
    invocation_id = f"repro-{label}-{int(time.time())}"
    print(
        f"\n=== {label}: invoking energy_researcher in subprocess (id={invocation_id}) ...",
        flush=True,
    )
    wall_start = time.monotonic()
    try:
        result = await invoke_domain_researcher_in_subprocess(
            agent_config=agent_config,  # type: ignore[arg-type]
            sector=Sector.ENERGY,
            user_message=user_message,
            invocation_id=invocation_id,
            archive_root=_REPRO_ARCHIVE,
        )
    except Exception as exc:
        elapsed = time.monotonic() - wall_start
        print(f"{label}: FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}", flush=True)
        raise
    else:
        elapsed = time.monotonic() - wall_start
        print(
            f"{label}: OK after {elapsed:.1f}s: retry_count={result.retry_count}, "
            f"input_tokens={result.tokens_used.input_tokens}, "
            f"output_tokens={result.tokens_used.output_tokens}",
            flush=True,
        )


async def main() -> None:
    loaded = parse_loaded_config(Path("config"))
    agent_config = loaded.agents.agents[AgentName.energy_researcher]
    user_message = _latest_energy_user_message()
    print(f"user_message size: {len(user_message)} bytes", flush=True)
    print(f"agent model: {agent_config.model}", flush=True)

    await _invoke("call1", user_message, agent_config)
    await _invoke("call2", user_message, agent_config)


if __name__ == "__main__":
    _REPRO_ARCHIVE.mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
