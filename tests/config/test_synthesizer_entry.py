"""Verification tests for the `synthesizer` entry in `config/agents.yaml` (ALP-200).

The synthesizer entry was authored 2026-04-27 during the configuration-management
work tree's "Land analysis-layer prompts" quick win. These tests round-trip the
existing entry against the design contract documented in
`docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate,
`docs/design/03-analysis-layer/synthesizer.md` § Output, and
`docs/architecture/llm-integration.md` § Agent inventory, and confirm the
prompt path resolves and the empty `tools` list is intentional.
"""

from pathlib import Path
from typing import Any, cast

import yaml

from alphamind.config.models import AgentName, AgentsConfig, BaseAgentConfig

REPO_ROOT = Path(__file__).parent.parent.parent
AGENTS_YAML = REPO_ROOT / "config" / "agents.yaml"


def _load_synthesizer_entry() -> BaseAgentConfig:
    raw = cast(dict[str, Any], yaml.safe_load(AGENTS_YAML.read_text()))
    config = AgentsConfig.model_validate(raw)
    return config.agents[AgentName.synthesizer]


def test_synthesizer_entry_loads() -> None:
    """`BaseAgentConfig` validates the synthesizer entry without error and the
    five field values match the design contract."""
    entry = _load_synthesizer_entry()
    assert entry.model == "claude-sonnet-4-6"
    assert entry.prompt == "prompts/analysis/synthesizer.md"
    assert entry.latency_budget_seconds == 900
    assert entry.context_token_budget == 12000
    assert entry.output_token_budget == 14000


def test_synthesizer_prompt_path_exists() -> None:
    """The `prompt:` field resolves to a readable file under the repo root."""
    entry = _load_synthesizer_entry()
    prompt_path = REPO_ROOT / entry.prompt
    assert prompt_path.is_file()
    # Read confirms the file is openable, not a broken symlink or empty stub.
    assert prompt_path.read_text().strip()


def test_synthesizer_tools_empty() -> None:
    """`tools` is intentionally `[]`.

    The synthesizer's three portfolio-state tools and the downstream
    `retrieve_brief` tool are wired at the harness level via per-invocation
    closures over `SynthesizerPortfolioStateReader` and the per-call
    `RetrievalStore` (see ALP-114 § Notes for the orchestrator: per-invocation
    MCP wiring). The global `alphamind.analysis.tools.TOOLS` registry pattern
    that drives stateless analysis-layer agents does not apply here, so a
    future contributor must NOT "fix" this empty list by adding global tool
    names — those would be silently ignored and would mask the per-call wiring.
    """
    entry = _load_synthesizer_entry()
    assert entry.tools == []
