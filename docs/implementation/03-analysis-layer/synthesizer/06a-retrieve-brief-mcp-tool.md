---
status: not_started
completed_date:
commit_id:
---

# 06a — `retrieve_brief` MCP tool

## Goal

Wrap the `RetrievalStore.lookup` Python-level interface in an MCP tool the Claude Agent SDK exposes to the decision-layer LLM agents (analyst, strategist, PM). The tool accepts a single `ref_id` argument and returns the corresponding section text from the synthesizer's per-invocation retrieval store. This is the mechanism by which the decision-layer agents drill into upstream brief detail without loading every brief into context.

The synthesizer itself does NOT call this tool — it produces the source material that powers it. The tool's allowlist consumers are the three decision-layer agents (in their own work trees, when they land).

## Reading

- `docs/architecture/llm-integration.md` § Tool registration — MCP `@tool` registration pattern, return-shape convention (`{"content": [{"type": "text", "text": ...}]}`)
- `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — the contract this tool implements
- `docs/design/04-decision-layer/analyst.md` § Source brief retrieval — the analyst-side workflow the tool serves
- `docs/design/04-decision-layer/strategist.md` § Source brief retrieval — same for strategist
- `docs/design/04-decision-layer/portfolio-manager.md` § Retrieval tools — same for PM
- `docs/design/testing/llm-output-validation.md` § Layer 3 — Referential integrity — the consumer-side validator that uses `RetrievalStore.lookup` directly (Python-level, not via this tool)

## Depends on

- 05a (`RetrievalStore`, `lookup`)

## Scope

In scope: under `src/alphamind/analysis/synthesizer/` —

- `tools/retrieve_brief.py` defining:

  - A factory function `def make_retrieve_brief_tool(store: RetrievalStore)` returning a Claude Agent SDK `@tool`-decorated async callable. The factory pattern is necessary because the SDK's `@tool` decorator binds the function at decoration time; the per-invocation `RetrievalStore` is not available until runtime, so the runner (story 10) constructs the tool fresh per invocation and passes the per-invocation store via closure.

  - The tool name is `retrieve_brief`; description: `"Retrieve a section of an upstream analysis brief by reference ID (e.g., SA-TECH-3, QR-4, AR-2, CR-1)."`; argument schema: `{"ref_id": str}`.

  - Behavior:
    - Validate `ref_id` is a string — the SDK schema check covers shape, but a defensive `isinstance` guard with a clear error message is useful when a non-string slips through.
    - Call `store.lookup(ref_id)`.
    - If a section text is returned: return `{"content": [{"type": "text", "text": section_text}]}`.
    - If `None`: return a structured error response per the SDK's tool-error convention. The agent receives the error and the agent's prompt instructs it to either (a) try a different reference, (b) reason without the source detail and acknowledge in its narrative. The error response must NOT raise an exception that propagates up — the SDK treats raised exceptions as tool-use errors per `llm-agent-failure-handling.md § Tool-use error`, and "this reference doesn't exist" is a normal result, not an SDK error.
    - The error response shape: `{"content": [{"type": "text", "text": f"Reference '{ref_id}' not found in this invocation's retrieval store. Known prefixes: SA-TECH, SA-FIN, SA-ENERGY, QR, QR-CW, AR, CR. Check the synthesizer brief for valid references."}], "is_error": True}`. The `is_error: True` flag is the SDK's standard convention; the agent prompt distinguishes tool-error from successful-but-empty results.

  - The factory does NOT mutate global state. It does NOT keep a registry. The SDK reads the decorated callable; the runner re-creates the tool fresh each invocation.

- Unit tests under `tests/analysis/synthesizer/`:
  - The factory returns a callable whose name is `retrieve_brief` (verify via the SDK's tool-introspection surface or by inspecting the decorated function's `__name__` / `_tool_name` attribute, depending on what the SDK exposes).
  - Calling the tool with a known `ref_id` returns the expected `{"content": [{"type": "text", "text": ...}]}` payload.
  - Calling the tool with an unknown `ref_id` returns the documented error payload with `is_error: True` and does not raise.
  - The error message includes the offending `ref_id` and the list of known prefixes.
  - Calling the tool with a non-string argument (e.g., an integer slipped through) returns a clear error rather than raising a `TypeError`.
  - Multiple tools constructed from different `RetrievalStore` instances do not share state — calling one returns from its own store regardless of what the other contains.

Out of scope:
- Wiring the tool into specific agent SDK options — the synthesizer's harness (story 08) does not need this tool (the synthesizer doesn't call it). The decision-layer work trees (analyst, strategist, PM — not yet implemented) wire it into their own `ClaudeAgentOptions(allowed_tools=[...])` when they land.
- The Python-level Layer 3 validator that calls `RetrievalStore.lookup` directly (Layer 3 lives in the consumer agents' work trees per `llm-output-validation.md § Per-agent surface mapping`).
- Tool-use logging or telemetry beyond what the SDK captures by default (the harness story 08 covers diagnostic preservation for the synthesizer's own SDK calls).

## Notes

The factory pattern (rather than a plain top-level `@tool`-decorated function) is the cleanest way to inject the per-invocation retrieval store. The Agent SDK's `@tool` decorator typically expects a free function; binding to a per-invocation object via `functools.partial` or a class method usually works, but the factory closure is the most readable and avoids the ambiguity of "is the closure-captured store fresh on each invocation?" — by construction, yes, because the runner calls the factory each time.

The error response with `is_error: True` is preferred over raising an exception because tool-use exceptions in the SDK are "I cannot recover from this" semantics per `llm-agent-failure-handling.md`. A missing reference is recoverable: the agent reads the error and decides what to do next (try another reference, narrate without the detail). Treating it as a normal-result-with-error-flag matches that semantics.

The error message lists the known prefixes (SA-TECH, SA-FIN, SA-ENERGY, QR, QR-CW, AR, CR) rather than the full set of available reference IDs. Listing the full set would be 50+ IDs in a typical invocation — too noisy for the agent to act on, and a leak of upstream-brief structure that the prefix list captures more compactly. The synthesizer brief is the authoritative source for available IDs; the error message points the agent back at it.

The Layer 3 validator in the decision-layer work trees (when they land) will NOT use this MCP tool — Layer 3 is a Python-level check at the LLM-output validation seam, not an in-LLM tool call. Both surfaces use the same `RetrievalStore.lookup` underneath; the MCP tool is the LLM-runtime path, the validator is the post-output path. Story 05a's `lookup` is the shared primitive.

Per [`feedback_simplify_before_building.md`](../../../../.claude/projects/-Users-jatassi-Git-AlphaMind/memory/feedback_simplify_before_building.md), this story does NOT pre-build a tool registry or a "tools manifest" file. The factory is the registration; the runner (story 10) calls it at the right moment. If a future work tree needs a multi-agent tool registry, that's the time to build one.

The argument-schema TypeScript-style `{"ref_id": str}` is the SDK's convention; verify against the SDK's actual decorator signature at implementation time. Some SDK versions accept Pydantic models for the schema; if so, an explicit `class RetrieveBriefArgs(BaseModel): ref_id: str` is also acceptable and arguably safer.

## Acceptance criteria

- [ ] `make_retrieve_brief_tool(store)` returns an SDK `@tool`-decorated async callable named `retrieve_brief`.
- [ ] The tool accepts a `ref_id: str` argument per the SDK's schema convention.
- [ ] Successful lookup returns `{"content": [{"type": "text", "text": section_text}]}`.
- [ ] Unknown `ref_id` returns `{"content": [{"type": "text", "text": "..."}], "is_error": True}` with the `ref_id` and the known-prefix list in the message; does not raise.
- [ ] Non-string argument returns a clear error rather than raising.
- [ ] Tools created from different `RetrievalStore` instances do not share state.
- [ ] The factory does not mutate global state and does not keep an internal registry.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
