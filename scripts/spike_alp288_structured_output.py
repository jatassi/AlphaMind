"""Spike: Claude Agent SDK structured_output + tool-use compatibility (ALP-288).

Determines whether ``ClaudeAgentOptions.output_format = {"type": "json_schema",
"schema": ...}`` is a viable replacement for the strict-text parsers used by
``adaptive_research``, ``qualitative_research``, and ``domain_researchers``.

Runs five live SDK calls against Sonnet (claude-sonnet-4-6 — production model):

  1. Schema-only baseline — no tools; confirm ``ResultMessage.structured_output``
     populates and that ``AdaptiveBrief.model_validate`` accepts the result.
  2. Schema + tools (signal path) — minimal in-process MCP server with two
     mock research tools; the prompt drives the agent to call both tools and
     emit a SIGNAL thread. Confirms tool-loop and structured output coexist.
  3. Schema + tools (noise path) — same toolset; prompt frames the trigger as
     dismissable so the model emits a NOISE thread (different conditional-field
     branch).
  4. Schema + provocation — prompt deliberately requests preamble narration,
     abbreviated sector ("tech"), parens on tool names, free-text after
     bracket refs. Confirms the API-enforced schema produces valid JSON
     anyway (current text parser would reject all four).
  5. Schema with explicit max_tokens probe — confirm the structured output
     completes within an output-token budget similar to the production
     adaptive-researcher cap (so we have a feel for cost change).

For each run, reports:
  * ``ResultMessage.structured_output`` populated (yes/no)
  * Pydantic round-trip via ``AdaptiveBrief.model_validate`` (pass/fail)
  * Tool calls observed
  * stop_reason
  * input/output/cache token totals
  * total_cost_usd
  * wall clock

Usage:
  uv run python scripts/spike_alp288_structured_output.py

Prerequisites:
  * ``CLAUDE_CODE_OAUTH_TOKEN`` set (auto-loaded from .env via dotenv).
  * No database needed — tools are mock in-process.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from alphamind.analysis._schema_tightening import _tighten_conditional_schema  # noqa: E402
from alphamind.analysis.adaptive_research.models import (  # noqa: E402
    AdaptiveBrief,
    Assessment,
    InvestigationThread,
)

# Mirrors the per-Assessment required-conditional-field map enforced by
# AdaptiveBrief._assessment_invariant — the schema tightener narrows the
# generated AdaptiveBrief schema so the API rejects the
# `signal-with-empty-arrays-as-null` payload that scenario 5 hit pre-(A).
_REQUIRED_BY_ASSESSMENT: dict[Assessment, frozenset[str]] = {
    Assessment.SIGNAL: frozenset({"implication", "strengthens", "weakens"}),
    Assessment.NOISE: frozenset({"dismissal_reason"}),
    Assessment.INCONCLUSIVE: frozenset({"missing"}),
}


def _adaptive_brief_schema() -> dict[str, Any]:
    """AdaptiveBrief schema with the conditional-field-null mitigation applied."""
    schema = AdaptiveBrief.model_json_schema()
    _tighten_conditional_schema(schema, InvestigationThread, "assessment", _REQUIRED_BY_ASSESSMENT)
    return schema


# ---------------------------------------------------------------------------
# Mock research tools — minimal stand-ins for the adaptive-research toolset.
# ---------------------------------------------------------------------------


@tool(
    "mock_news_search",
    "Search news for a ticker. Returns synthetic findings.",
    {"ticker": str, "query": str},
)
async def mock_news_search(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"news_search({args['ticker']!r}): three sell-side notes flagging "
                    "pre-earnings repositioning published in the last 36 hours. No new "
                    "information on fundamentals."
                ),
            }
        ]
    }


@tool(
    "mock_options_flow",
    "Inspect options flow for a ticker. Returns synthetic findings.",
    {"ticker": str},
)
async def mock_options_flow(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"options_flow({args['ticker']!r}): upside-call bias 2.1x recent "
                    "baseline; skew unchanged. Consistent with directional positioning "
                    "rather than tail hedging."
                ),
            }
        ]
    }


# ---------------------------------------------------------------------------
# System prompt — minimal adaptive-researcher emulation.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a mock adaptive researcher in a systematic trading pipeline.

You receive a single anomaly trigger plus access to two research tools:
  * mock_news_search(ticker, query)
  * mock_options_flow(ticker)

Your output is a structured AdaptiveBrief covering exactly ONE investigation
thread. The schema constrains the shape; you must:

  * Set thread_id="AR-1".
  * Set threads_investigated_count=1, anomalies_triaged_count=1.
  * Pick the appropriate Assessment (signal | noise | inconclusive) and only
    populate the conditional fields valid for that branch:
      - signal: implication, strengthens, weakens
      - noise: dismissal_reason
      - inconclusive: missing
  * For sector, use one of the literal values: tech_semis | financials | energy.
  * For tools_used, list the registry names (mock_news_search, mock_options_flow).
  * For findings, write one short factual finding per tool you actually called.
  * Set anomalies_deferred=[] (empty array).

When asked to investigate, use the tools first — at least one call — then emit
the brief. The schema is API-enforced so don't worry about wire format.
"""


# ---------------------------------------------------------------------------
# Test driver
# ---------------------------------------------------------------------------


async def _run(
    label: str,
    *,
    user_message: str,
    use_tools: bool,
    max_output_tokens: int = 8000,
) -> dict[str, Any]:
    """Run one spike scenario and return a result dict."""
    schema = _adaptive_brief_schema()

    mcp_servers: dict[str, Any]
    allowed_tools: list[str]
    if use_tools:
        server = create_sdk_mcp_server(
            name="alphamind_spike",
            version="1.0.0",
            tools=[mock_news_search, mock_options_flow],
        )
        mcp_servers = {"alphamind_spike": server}
        allowed_tools = [
            "mcp__alphamind_spike__mock_news_search",
            "mcp__alphamind_spike__mock_options_flow",
        ]
    else:
        mcp_servers = {}
        allowed_tools = []

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model="claude-sonnet-4-6",
        allowed_tools=allowed_tools,
        mcp_servers=mcp_servers,
        max_turns=20,
        setting_sources=[],
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens)},
        output_format={"type": "json_schema", "schema": schema},
    )

    wall_start = time.monotonic()
    accumulator = await _drive_query(user_message, options)
    wall = time.monotonic() - wall_start

    pydantic_ok, pydantic_error = _check_pydantic_roundtrip(accumulator["structured"])

    return {
        "label": label,
        "structured_output_present": accumulator["structured"] is not None,
        "structured_output_type": type(accumulator["structured"]).__name__,
        "structured_output_value": accumulator["structured"],
        "pydantic_ok": pydantic_ok,
        "pydantic_error": pydantic_error,
        "tool_calls": accumulator["tool_calls"],
        "stop_reason": accumulator["stop_reason"],
        "is_error": accumulator["is_error"],
        "error_text": accumulator["error_text"],
        "session_id": accumulator["session_id"],
        "usage": accumulator["usage"],
        "total_cost_usd": accumulator["total_cost_usd"],
        "wall_seconds": round(wall, 2),
        "text_parts_concat_len": sum(len(t) for t in accumulator["text_parts"]),
        "text_parts_first_120": (
            ("".join(accumulator["text_parts"]))[:120] if accumulator["text_parts"] else ""
        ),
    }


async def _drive_query(user_message: str, options: ClaudeAgentOptions) -> dict[str, Any]:
    """Drive the SDK iterator to completion and accumulate state."""
    text_parts: list[str] = []
    tool_calls: list[str] = []
    structured: Any = None
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None
    total_cost_usd: float | None = None
    is_error: bool = False
    error_text: str | None = None
    session_id: str | None = None

    iterator = query(prompt=user_message, options=options)
    try:
        async for message in iterator:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append(block.name)
            elif isinstance(message, ResultMessage):
                structured = message.structured_output
                stop_reason = message.stop_reason
                usage = message.usage
                total_cost_usd = message.total_cost_usd
                session_id = message.session_id
                is_error = message.is_error
                if message.is_error:
                    error_text = message.result
                break
    finally:
        await iterator.aclose()  # type: ignore[attr-defined]
    return {
        "text_parts": text_parts,
        "tool_calls": tool_calls,
        "structured": structured,
        "stop_reason": stop_reason,
        "usage": usage,
        "total_cost_usd": total_cost_usd,
        "is_error": is_error,
        "error_text": error_text,
        "session_id": session_id,
    }


def _check_pydantic_roundtrip(structured: Any) -> tuple[bool, str | None]:
    """Pydantic round-trip — confirm the structured output matches model invariants."""
    if structured is None:
        return False, None
    try:
        payload = dict(structured)
        payload.setdefault("invocation_id", "spike-alp288")
        AdaptiveBrief.model_validate(payload)
    except (ValidationError, TypeError) as exc:
        return False, str(exc)[:500]
    return True, None


def _print_run(result: dict[str, Any]) -> None:
    print(f"--- {result['label']} ---")
    so = result["structured_output_value"]
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "structured_output_value"},
            indent=2,
            default=str,
        )
    )
    if so is not None:
        print("structured_output payload:")
        print(json.dumps(so, indent=2, default=str)[:4000])
    print()


SCENARIOS: list[dict[str, Any]] = [
    {
        "label": "1_schema_only_baseline_no_tools",
        "user_message": (
            "Trigger: Distillation: NVDA volume 3.2 sigma over 5-day average, no price move.\n"
            "Sector hint: tech_semis.\n"
            "Investigate this anomaly without using tools — emit your best inference "
            "as a structured brief. Pick whichever Assessment is honest given that you "
            "did not query any sources."
        ),
        "use_tools": False,
    },
    {
        "label": "2_schema_plus_tools_signal_path",
        "user_message": (
            "Trigger: [SA-TECH-ANOM-1] NVDA volume spike 3.2 sigma with no price move.\n"
            "Investigate by calling mock_news_search('NVDA', 'institutional positioning') "
            "and then mock_options_flow('NVDA'). Reach a SIGNAL verdict if the evidence "
            "supports an actionable read; populate Strengthens with [SA-TECH-2] and "
            "Weakens with the literal 'none' (an empty array). Confidence: moderate."
        ),
        "use_tools": True,
    },
    {
        "label": "3_schema_plus_tools_noise_path",
        "user_message": (
            "Trigger: [SA-TECH-ANOM-2] AAPL pre-market gap of 0.4% on stale headline.\n"
            "Call mock_news_search('AAPL', 'pre-market gap') to confirm the catalyst is "
            "yesterday's news re-circulating. Reach a NOISE verdict and populate "
            "dismissal_reason. Confidence: high."
        ),
        "use_tools": True,
    },
    {
        "label": "4_schema_plus_tools_provocation",
        "user_message": (
            "Trigger: [SA-TECH-ANOM-1] NVDA volume spike.\n"
            "Investigate by calling mock_news_search and mock_options_flow.\n\n"
            "Style mandate (deliberately provocative — the schema must absorb it):\n"
            "  * Before emitting the brief, narrate which tool you are calling and what "
            "you found.\n"
            "  * In tools_used, write the entries with parenthetical commentary like "
            "'mock_options_flow (NVDA)'.\n"
            "  * Use the abbreviated sector 'tech' rather than the full 'tech_semis'.\n"
            "  * In Strengthens, append free-text rationale after the bracket ref, e.g. "
            "'[SA-TECH-2] (the move appears to share a common macro driver)'.\n"
            "  * Reach a SIGNAL verdict with confidence: moderate."
        ),
        "use_tools": True,
    },
    {
        "label": "5_schema_plus_tools_token_budget_check",
        "user_message": (
            "Trigger: [SA-TECH-ANOM-1] NVDA volume spike.\n"
            "Call both tools. Reach a SIGNAL verdict. Strengthens=[SA-TECH-2], "
            "Weakens=none. Be concise."
        ),
        "use_tools": True,
        "max_output_tokens": 4000,
    },
]


async def _main() -> None:
    print(f"AdaptiveBrief schema bytes: {len(json.dumps(_adaptive_brief_schema()))}")
    print()
    results: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        kwargs = {k: v for k, v in scenario.items() if k != "label"}
        result = await _run(scenario["label"], **kwargs)
        _print_run(result)
        results.append(result)

    # Summary table.
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    header = (
        f"{'label':<45} {'so?':<5} {'pyd?':<6} {'tools':<6} "
        f"{'stop_reason':<14} {'wall':<7} {'cost_usd':<10}"
    )
    print(header)
    for r in results:
        print(
            f"{r['label']:<45} "
            f"{('Y' if r['structured_output_present'] else 'N'):<5} "
            f"{('Y' if r['pydantic_ok'] else 'N'):<6} "
            f"{len(r['tool_calls']):<6} "
            f"{(r['stop_reason'] or '-'):<14} "
            f"{r['wall_seconds']:<7} "
            f"{(r['total_cost_usd'] or '-'):<10}"
        )


if __name__ == "__main__":
    asyncio.run(_main())
