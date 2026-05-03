"""End-to-end live integration test for the qualitative researcher (ALP-253).

Skipped by default. Set ``RUN_LIVE_LLM_TESTS=1`` to run against the
populated production database with a live ``CLAUDE_CODE_OAUTH_TOKEN``.

The test asserts the structural invariants spelled out in story ALP-253
§ Scope § Automated test:

- The runner returns a :class:`QualitativeResearcherResult` with no exceptions.
- ``result.brief.threads`` has at least one entry.
- ``result.brief.sentiment_snapshot`` has populated ``extremes``,
  ``divergences``, ``regime`` fields.
- ``result.tokens_used.input_tokens > 0`` and
  ``result.tokens_used.output_tokens > 0``.
- ``result.wall_clock_seconds < agent_config.latency_budget_seconds``.
- ``result.tool_calls_used <= agent_config.cumulative_tool_call_limit``.
- ``result.news_digest.entries`` is non-empty (assuming the test DB has
  news in the lookback window).
- The diagnostic archive contains all expected files.

The harness's no-SDK-required paths (parser, validator, retry) are
covered by ``tests/analysis/qualitative_research/test_harness.py``;
this test exercises the live path only.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alphamind.analysis.qualitative_research.runner import (
    QualitativeResearcherResult,
    run_qualitative_researcher,
)
from alphamind.config.models.agents import AdaptiveAgentConfig, AgentName
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.scripts._common import load_universe_scope
from alphamind.scripts.verify_qualitative_researcher import (
    _REQUIRED_DIAGNOSTIC_FILES,
    _diagnostic_dir,
    _load_agents_config,
    _load_recent_regime_label,
)

_LIVE_FLAG = "RUN_LIVE_LLM_TESTS"
_OAUTH_FLAG = "CLAUDE_CODE_OAUTH_TOKEN"


pytestmark = pytest.mark.skipif(
    os.environ.get(_LIVE_FLAG) != "1",
    reason=f"Live LLM test gated on {_LIVE_FLAG}=1; skipped by default.",
)


def _resolve_archive_root() -> Path:
    """Use a per-run temp dir under the repo's .archive/ for traceability.

    A fresh subdirectory keeps the diagnostic-archive assertions
    deterministic across multiple invocations of the test.
    """
    repo_root = Path(__file__).resolve().parents[3]
    base = repo_root / ".archive" / "test-e2e-qualitative-researcher"
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_qualitative_researcher_e2e_live() -> None:
    """Run the qualitative researcher against the live SDK + populated DB."""
    if not os.environ.get(_OAUTH_FLAG):
        pytest.skip(f"{_OAUTH_FLAG} not set — required for live SDK invocation.")

    now = datetime.now(tz=UTC)
    invocation_id = now.strftime("%Y%m%dT%H%M%SZ") + "-e2e-qualitative-researcher"
    archive_root = _resolve_archive_root()
    universe = frozenset(load_universe_scope())

    agents_yaml = _load_agents_config()
    agent_name_str = AgentName.qualitative_researcher.value
    agent_config = agents_yaml.agents[AgentName.qualitative_researcher]
    assert isinstance(agent_config, AdaptiveAgentConfig), (
        "qualitative_researcher must be a tool-loop agent"
    )
    agents_config = {name.value: cfg for name, cfg in agents_yaml.agents.items()}

    engine = make_engine()
    factory = make_session_factory(engine)
    with factory() as session:
        regime_label, _source = _load_recent_regime_label(session)

        result: QualitativeResearcherResult = asyncio.run(
            run_qualitative_researcher(
                invocation_id=invocation_id,
                as_of=now,
                last_invocation_time=now - timedelta(hours=1),
                session=session,
                universal_regime_label=regime_label,
                universe=universe,
                agents_config=agents_config,
                archive_root=archive_root,
            )
        )

    # Brief shape.
    assert len(result.brief.threads) >= 1
    snapshot = result.brief.sentiment_snapshot
    assert snapshot.extremes
    assert snapshot.divergences
    assert snapshot.regime

    # Tokens used (live SDK should report non-zero).
    assert result.tokens_used.input_tokens > 0
    assert result.tokens_used.output_tokens > 0

    # Budget compliance.
    assert result.wall_clock_seconds < float(agent_config.latency_budget_seconds), (
        f"wall_clock {result.wall_clock_seconds:.2f}s "
        f">= latency_budget {agent_config.latency_budget_seconds}s"
    )
    assert result.tool_calls_used <= agent_config.cumulative_tool_call_limit, (
        f"tool_calls_used {result.tool_calls_used} "
        f"> cumulative_tool_call_limit {agent_config.cumulative_tool_call_limit}"
    )
    assert result.tokens_used.output_tokens <= agent_config.output_token_budget, (
        f"output_tokens {result.tokens_used.output_tokens} "
        f"> output_token_budget {agent_config.output_token_budget}"
    )

    # News digest is populated (assumes the DB has news in the lookback window).
    assert len(result.news_digest.entries) > 0, (
        "news_digest.entries is empty — verify the test DB has clustered news "
        "in the [as_of - 1h, as_of) window before re-running."
    )

    # Diagnostic archive is populated with the expected files.
    diag_dir = _diagnostic_dir(archive_root, invocation_id, agent_name_str)
    missing = [name for name in _REQUIRED_DIAGNOSTIC_FILES if not (diag_dir / name).exists()]
    assert not missing, f"diagnostic archive {diag_dir} missing files: {missing}"
