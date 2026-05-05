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


def _load_analyst_entry() -> BaseAgentConfig:
    raw = cast(dict[str, Any], yaml.safe_load(AGENTS_YAML.read_text()))
    config = AgentsConfig.model_validate(raw)
    return config.agents[AgentName.analyst]


def test_analyst_entry_loads() -> None:
    """`BaseAgentConfig` validates the analyst entry without error and the
    field values match the design contract."""
    entry = _load_analyst_entry()
    assert entry.model == "claude-opus-4-7"
    assert entry.prompt == "prompts/decision/analyst.md"
    assert entry.latency_budget_seconds == 180
    assert entry.context_token_budget == 8000
    # Each recommendation serializes to ~500-800 tokens with the rich nested
    # shape (instrument + entry_order + position_size + target +
    # invalidation_legs + 4 narrative fields + guardrail_validation_result
    # mirror). 1-3 recommendations sit at 1500-2500 tokens with no headroom
    # for retry or empty-day clarifications. 4000 leaves ~50% headroom.
    assert entry.output_token_budget == 4000


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
