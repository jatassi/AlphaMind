---
name: harness-telemetry-seam
description: How the analysis+decision LLM harness cores are wired (DiagState funnel, subprocess worker, DB session ownership) — for any story touching agent_calls/telemetry/provenance capture
metadata:
  type: project
---

The two shared harness cores cover 9 LLM agents: `analysis/_harness_core.py` (`invoke_sdk` + `DiagState` + `_SdkCallTracer`) backs domain_researchers (3 sectors), qualitative_research, adaptive_research, synthesizer; the 3 decision harnesses (analyst/strategist/PM) ALSO import `invoke_sdk`+`DiagState` from `analysis/_harness_core` (decision/_shared is thin — just `system_prompt_as_file` + `direction_display`). So "two cores" really = one shared core (`_harness_core.py`) + per-harness wiring.

**Single funnel.** `DiagState.write(success, wall_clock_seconds, stop_reason)` (sync) is THE terminal-path funnel called on every success+failure across all harnesses, including inside `invoke_sdk`'s `_record_failure`. ~14 `diag.write(...)` call sites across harnesses + 1 inside invoke_sdk. To add capture without touching 14 sites: extend `DiagState.write()` itself (it already does mkdir+write_text for prompt.md/response*.md/errors.json/metadata.json).

**Subprocess boundary (ALP-650).** Every harness invocation runs in a fresh `python -m alphamind.analysis._sdk_subprocess_worker` process (Windows SDK state-leak workaround). `_sdk_subprocess.py` builds a JSON payload (agent_config dump, user_message, invocation_id, as_of, archive_root, ...) → worker imports the harness, runs one `invoke_<agent>`, serializes result/failure to stdout. archive_root + invocation_id + as_of all cross in; the diag FILES are written subprocess-side. The worker already opens `make_async_engine()/make_async_session_factory()` (and sync `make_engine`) against `DATABASE_PATH` for agents that need DB-backed MCP tools (qualitative, PM gets an InvocationHandle). So a DB write (e.g. agent_calls row) belongs subprocess-side, in the worker, where the session lifecycle already lives — NOT in the parent (would require threading every captured signal back across the transport).

**Provenance layout differs from diag layout.** diag files: `invocation_archive_dir(archive_root, as_of, invocation_id)/<layer>/<agent>/` (date-partitioned, `_kernel/archive_layout.py`). agent_calls provenance: `data/provenance/invocations/{invocation_id}/agent_calls/{agent_call_id}/{system_prompt.md,output_schema.json,tools_definition.json,output.json}` — NOT date-partitioned, NOT layered, keyed by a per-call id. Same write idiom, different root.

**DB test substrate.** `tests/state/_fk_substrate.py` has `stub_invocation_row` + `stub_process_lifetime_row`; agent_calls FK → invocations(ON DELETE RESTRICT) → process_lifetimes, so seed plt then invocation before inserting an agent_calls row. Async fixture pattern (on-disk sqlite, sync create_all+seed, then make_async_engine) in `tests/state/test_agent_calls_table.py`.

**AgentCallRecord (02a/ALP-873) is a SUBSET of the design doc.** state-persistence.md lists call_ordinal, model_alias_requested, api_version_header, beta_features, separate output_schema_git_sha/content_hash, tools_definition_hash — the as-built `state/tables/agent_calls.py` record does NOT have these. Use the as-built shape, don't redefine it. error_class enum: timeout/malformed_output/context_overflow/model_api_error/tool_use_error.
