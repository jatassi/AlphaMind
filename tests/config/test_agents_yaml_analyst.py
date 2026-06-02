"""Verification tests for the `analyst` entry in `config/agents.yaml` (ALP-291).

The analyst entry was authored 2026-04-27 during the configuration-management
work tree's "Land decision-layer prompts" quick win. These tests round-trip the
existing entry against the design contract documented in
`docs/design/04-decision-layer/analyst.md` § Pre-submission guardrail validation,
§ Source brief retrieval, and `docs/design/cost-and-rate-limit-modeling.md`
§ Per-invocation agent surface, and confirm the prompt path resolves and the
two-tool contract matches the design.
"""

from pathlib import Path
from typing import Any, cast

import yaml

from alphamind.config.models import AgentName, AgentsConfig, BaseAgentConfig

REPO_ROOT = Path(__file__).parent.parent.parent
AGENTS_YAML = REPO_ROOT / "config" / "agents.yaml"


def _load_agent_entry(name: AgentName) -> BaseAgentConfig:
    raw = cast(dict[str, Any], yaml.safe_load(AGENTS_YAML.read_text()))
    config = AgentsConfig.model_validate(raw)
    return config.agents[name]


def _load_analyst_entry() -> BaseAgentConfig:
    return _load_agent_entry(AgentName.analyst)


def test_analyst_entry_loads() -> None:
    """`BaseAgentConfig` validates the analyst entry without error and the
    field values match the design contract."""
    entry = _load_analyst_entry()
    assert entry.model == "claude-opus-4-8"
    assert entry.prompt == "prompts/decision/analyst.md"
    assert entry.latency_budget_seconds == 750
    assert entry.context_token_budget == 12000
    assert entry.output_token_budget == 150000


def test_analyst_prompt_path_exists() -> None:
    """The `prompt:` field resolves to a readable file under the repo root."""
    entry = _load_analyst_entry()
    prompt_path = REPO_ROOT / entry.prompt
    assert prompt_path.is_file()
    # Read confirms the file is openable, not a broken symlink or empty stub.
    assert prompt_path.read_text().strip()


def test_analyst_tools_contract() -> None:
    """`tools` matches the two-tool contract from the analyst design doc.

    The analyst uses exactly two tools:
    - `retrieve_brief`: accepts a reference ID and returns the corresponding
      section from the original research brief (analyst.md § Source brief
      retrieval).
    - `validate_guardrail`: accepts proposed instrument, direction, and size;
      computes delta-adjusted exposure; returns per-rule pass/fail with
      headroom (analyst.md § Pre-submission guardrail validation).
    """
    entry = _load_analyst_entry()
    assert entry.tools == ["retrieve_brief", "validate_guardrail"]


# ---------------------------------------------------------------------------
# Portfolio manager entry
# ---------------------------------------------------------------------------


def _load_pm_entry() -> BaseAgentConfig:
    return _load_agent_entry(AgentName.portfolio_manager)


class TestAgentsYamlPortfolioManagerEntry:
    """Verify `config/agents.yaml`'s `portfolio_manager` entry matches the
    design contract (ALP-321): model, prompt, raised budgets, four-tool set."""

    def test_pm_entry_loads(self) -> None:
        """`BaseAgentConfig` validates the portfolio_manager entry and all
        field values match the design contract with raised budgets."""
        entry = _load_pm_entry()
        assert entry.model == "claude-opus-4-8"
        assert entry.prompt == "prompts/decision/pm.md"
        assert entry.latency_budget_seconds == 1350
        assert entry.context_token_budget == 24000
        assert entry.output_token_budget == 36000

    def test_pm_prompt_path_exists(self) -> None:
        """The `prompt:` field resolves to a readable file under the repo root."""
        entry = _load_pm_entry()
        prompt_path = REPO_ROOT / entry.prompt
        assert prompt_path.is_file()
        assert prompt_path.read_text().strip()

    def test_pm_tools_contract(self) -> None:
        """`tools` matches the five-tool contract from the PM design doc.

        The portfolio manager uses five tools:
        - `retrieve_brief`: source brief retrieval.
        - `validate_guardrail`: pre-submission guardrail validation (single).
        - `validate_guardrail_batch`: pre-submission guardrail validation for a
          coordinated multi-position remediation package (ALP-625).
        - `get_thesis_components`: fetches thesis breakdown per instrument.
        - `submit_envelope`: submits the final order envelope.
        """
        entry = _load_pm_entry()
        assert set(entry.tools) == {
            "retrieve_brief",
            "validate_guardrail",
            "validate_guardrail_batch",
            "get_thesis_components",
            "submit_envelope",
        }
