# 01 — Verify config/agents.yaml entry + raise budgets

## Goal

Verify that the `portfolio_manager` slot in `config/agents.yaml` matches the design contract (`docs/design/configuration-management.md` § agents.yaml + the PM-relevant entries in `prompts/decision/pm.md` + `agents.yaml.lock`), and raise three numeric budgets per the operator's drafting-time directive: `latency_budget_seconds: 240 → 600`, `context_token_budget: 12000 → 16000`, `output_token_budget: 3000 → 16000`. Mirrors strategist story 01 (<issue id="1741250f-72ca-42ad-8848-0a081fb60974">ALP-301</issue>).

## Reading

* `config/agents.yaml` — current `portfolio_manager` entry (lines around the entry; mid-file).
* `docs/design/configuration-management.md` § `agents.yaml` — the canonical schema for one agent entry.
* `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate — confirms Opus model selection and budget rationale; provides the framing operator referenced when directing the bump.
* `src/alphamind/config/models/agents.py` — `BaseAgentConfig`, `AgentName.portfolio_manager`, `AgentsConfig` validator. Ensures the raised budgets pass model validation.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decision (B) names the specific values.
* Sibling pattern: strategist story 01 ([ALP-301](<https://linear.app/alphamind-jatassi/issue/ALP-301>), *done*) for the analogous yaml-edit shape.

## Depends on

None — Wave 1 dispatch.

## Scope

In scope: `config/agents.yaml` (single edit) and `tests/config/test_agents_yaml.py` (verification test). No code under `src/alphamind/decision/portfolio_manager/`.

### 1\. Verify the existing `portfolio_manager` entry's structure

Read `config/agents.yaml` and confirm the `portfolio_manager` slot exists with these fields populated and matching the design:

* `model: claude-opus-4-7`
* `prompt: prompts/decision/pm.md`
* `tools: [retrieve_brief, validate_guardrail, get_thesis_components, submit_envelope]` (exact order is not load-bearing, but the four-tool set is).

If any of these is wrong (model is not Opus, prompt path is wrong, the tools list is missing or has a different set), surface to the operator per the parent issue's surfacing condition for verification stories — do not silently rewrite.

### 2\. Raise three numeric budgets

Edit the `portfolio_manager` entry's three numeric fields:

* `latency_budget_seconds: 240` → `600`
* `context_token_budget: 12000` → `16000`
* `output_token_budget: 3000` → `16000`

Add a comment block above the entry explaining the rationale (token-cost framing from `cost-and-rate-limit-modeling.md`, operator directive at drafting time, the four-MCP-server tool-loop runtime, and the synchronous-rejection retry path). Mirror the comment-style of the analyst / strategist entries' budget-bump comments — e.g., the strategist entry's comment naming the regime-transition / 6-position bundle observation.

### 3\. Add or update the verification test

Under `tests/config/`, ensure a test exists asserting the `portfolio_manager` entry's full shape — model, prompt path, tools set, the three budgets at their raised values. If a test already exists at this site (mirroring `tests/config/test_agents_yaml.py::TestAgentsYamlAnalystEntry`), extend it with a `TestAgentsYamlPortfolioManagerEntry` test class. If not, add one in the same style.

The test must:

* Load `config/agents.yaml` via `AgentsConfig.model_validate`.
* Assert `cfg.agents[AgentName.portfolio_manager].model == "claude-opus-4-7"`.
* Assert `cfg.agents[AgentName.portfolio_manager].latency_budget_seconds == 600`.
* Assert `cfg.agents[AgentName.portfolio_manager].context_token_budget == 16000`.
* Assert `cfg.agents[AgentName.portfolio_manager].output_token_budget == 16000`.
* Assert `cfg.agents[AgentName.portfolio_manager].tools` contains `{"retrieve_brief", "validate_guardrail", "get_thesis_components", "submit_envelope"}` as a set.

### Out of scope

* Verifying the prompt content — that is story 02's responsibility.
* Verifying the four MCP tool wrappers exist — that is stories 04, 05, and the harness wiring (story 07).
* Adding new agent entries — only the `portfolio_manager` slot is touched.

## Acceptance criteria

- [ ] `config/agents.yaml`'s `portfolio_manager` entry has `latency_budget_seconds: 600`, `context_token_budget: 16000`, `output_token_budget: 16000`.
- [ ] `config/agents.yaml`'s `portfolio_manager` entry's other fields (`model`, `prompt`, `tools`) are unchanged from their pre-edit values.
- [ ] A comment block above the `portfolio_manager` entry names the rationale for the budget-bump (operator directive + four-MCP-server tool-loop + synchronous-rejection retry path).
- [ ] `uv run pytest tests/config/ -n auto` passes.
- [ ] The test under `tests/config/test_agents_yaml.py` (or sibling) explicitly asserts the three raised budget values.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass without changes.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/config/ -n auto` — the `TestAgentsYamlPortfolioManagerEntry` test class (or extension to an existing class) passes. Visually inspect the diff: only the three numeric fields plus a comment block changed in `config/agents.yaml`; only the test file changed in `tests/`.
