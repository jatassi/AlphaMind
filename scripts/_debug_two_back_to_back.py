"""Two-back-to-back SDK call diagnostic.

Extends ``scripts/_debug_energy_isolated.py`` to invoke
``invoke_domain_researcher`` TWICE in sequence in the same Python process,
using the byte-identical captured ``user_message.md`` from the most recent
failed verify run. If the second call stalls, the
"first-call-succeeds, subsequent-may-stall" process-state-leakage
hypothesis (see docs/_investigation/debug-e2e-sdk-stall.md) is confirmed
and we have a minimal upstream repro.

Each call uses a distinct invocation_id so the diagnostic archive captures
both attempts side-by-side under ``.archive/repro-two-back-to-back/``.

Usage:

    set -a && source .env && set +a && \\
        uv run python scripts/_debug_two_back_to_back.py
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
_REPRO_ARCHIVE = Path(".archive/repro-two-back-to-back")


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


async def _invoke(label: str, user_message: str, agent_config: object) -> None:
    invocation_id = f"repro-{label}-{int(time.time())}"
    print(f"\n=== {label}: invoking energy_researcher (invocation_id={invocation_id}) ...")
    wall_start = time.monotonic()
    try:
        result = await invoke_domain_researcher(
            agent_config=agent_config,  # type: ignore[arg-type]
            sector=Sector.ENERGY,
            user_message=user_message,
            invocation_id=invocation_id,
            archive_root=_REPRO_ARCHIVE,
        )
    except Exception as exc:
        elapsed = time.monotonic() - wall_start
        print(f"{label}: FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        raise
    else:
        elapsed = time.monotonic() - wall_start
        print(
            f"{label}: OK after {elapsed:.1f}s: retry_count={result.retry_count}, "
            f"input_tokens={result.tokens_used.input_tokens}, "
            f"output_tokens={result.tokens_used.output_tokens}"
        )


async def main() -> None:
    loaded = parse_loaded_config(Path("config"))
    agent_config = loaded.agents.agents[AgentName.energy_researcher]
    user_message = _latest_energy_user_message()
    print(f"user_message size: {len(user_message)} bytes")
    print(f"agent model: {agent_config.model}")
    print(f"latency_budget_seconds: {agent_config.latency_budget_seconds}")

    await _invoke("call1", user_message, agent_config)
    await _invoke("call2", user_message, agent_config)


if __name__ == "__main__":
    _REPRO_ARCHIVE.mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
