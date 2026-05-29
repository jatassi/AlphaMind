"""Verification tests for the `strategist` entry in `config/agents.yaml` (ALP-301).

The strategist entry was authored 2026-04-27 during the configuration-management
work tree's "Land decision-layer prompts" quick win. These tests round-trip the
existing entry against the design contract documented in
`docs/design/04-decision-layer/strategist.md` § Token budget and
`docs/design/cost-and-rate-limit-modeling.md` § Per-invocation agent surface,
and confirm the prompt path resolves and the two-tool contract matches the design.
"""

from pathlib import Path
from typing import Any, cast

import yaml

from alphamind.config.models import AgentName, AgentsConfig, BaseAgentConfig

REPO_ROOT = Path(__file__).parent.parent.parent
AGENTS_YAML = REPO_ROOT / "config" / "agents.yaml"


def _load_strategist_entry() -> BaseAgentConfig:
    raw = cast(dict[str, Any], yaml.safe_load(AGENTS_YAML.read_text()))
    config = AgentsConfig.model_validate(raw)
    return config.agents[AgentName.strategist]


def test_strategist_entry_loads() -> None:
    """`BaseAgentConfig` validates the strategist entry without error and the
    field values match the design contract."""
    entry = _load_strategist_entry()
    assert entry.model == "claude-opus-4-8"
    assert entry.prompt == "prompts/decision/strategist.md"
    assert entry.latency_budget_seconds == 900
    assert entry.context_token_budget == 8000
    assert entry.output_token_budget == 24000


def test_strategist_prompt_path_exists() -> None:
    """The `prompt:` field resolves to a readable file under the repo root."""
    entry = _load_strategist_entry()
    prompt_path = REPO_ROOT / entry.prompt
    assert prompt_path.is_file()
    # Read confirms the file is openable, not a broken symlink or empty stub.
    assert prompt_path.read_text().strip()


def test_strategist_tools_contract() -> None:
    """`tools` matches the three-tool contract from the strategist design doc.

    The strategist uses three tools:
    - `retrieve_brief`: accepts a reference ID and returns the corresponding
      section from the original research brief (strategist.md § Source brief
      retrieval).
    - `validate_guardrail`: accepts proposed instrument, direction, and size;
      computes delta-adjusted exposure; returns per-rule pass/fail with
      headroom (strategist.md § Pre-submission guardrail validation).
    - `validate_guardrail_batch`: the batch sibling used to validate a
      coordinated multi-position remediation as one transaction (ALP-625).
    All three tools are used in both primary and full-system modes per parent
    Issue decision (I).
    """
    entry = _load_strategist_entry()
    assert entry.tools == ["retrieve_brief", "validate_guardrail", "validate_guardrail_batch"]
