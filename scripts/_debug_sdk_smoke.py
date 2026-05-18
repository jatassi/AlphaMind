"""Bare-minimum claude_agent_sdk smoke test.

Calls ``claude_agent_sdk.query`` with a tiny prompt and dumps every
message that streams back so we can tell whether the SDK is currently
working AT ALL — independent of the AlphaMind harness layers.

Usage:

    set -a && source .env && set +a && \\
        uv run python scripts/_debug_sdk_smoke.py
"""

from __future__ import annotations

import asyncio
import time

from claude_agent_sdk import ClaudeAgentOptions, query


async def main() -> None:
    options = ClaudeAgentOptions(
        system_prompt="Reply with just the digit '4'.",
        model="claude-sonnet-4-6",
        tools=[],
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
        extra_args={"strict-mcp-config": None},
    )
    print("about to call claude_agent_sdk.query(...) ...", flush=True)
    wall_start = time.monotonic()
    msg_count = 0
    try:
        async for msg in query(prompt="What is 2+2?", options=options):
            elapsed = time.monotonic() - wall_start
            msg_count += 1
            print(f"[+{elapsed:6.1f}s] msg {msg_count}: {type(msg).__name__}", flush=True)
    except Exception as exc:
        elapsed = time.monotonic() - wall_start
        print(f"FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}", flush=True)
        raise
    elapsed = time.monotonic() - wall_start
    print(f"DONE after {elapsed:.1f}s ({msg_count} messages)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
