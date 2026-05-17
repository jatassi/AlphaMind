# 12b — Narrow bare except Exception handlers (~36 real sites)

## Goal

Address the ~36 unwarranted `except Exception` / `except BaseException` handlers across the codebase. The L4 scanner flagged 61 sites total; per-subdivision triage classified ~25 as warranted outermost-supervisor patterns (document with inline comments tying to runtime §G1 exception). The remaining ~36 swallow real errors and should narrow to specific exception classes, log with `exc_info=exc`, and `raise ... from exc` where re-raising.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — high-yield L4 row
* `.claude/skills/python-architecture/references/runtime.md` § G1 — narrow except handlers, re-raise with `from`
* Per-subdivision hot lists (audit triages):
  * data_sources: 12 sites (polygon/equity.py:157, polygon/options.py:99, polygon/corporate_actions.py:85, polygon/reference.py:69, fred/client.py:61, marketaux/client.py:64, finra/client.py:93, sec_edgar/rss.py:272, iborrowdesk/client.py:121,139, kalshi/client.py:73, polymarket/client.py:76)
  * execution: 13 sites (continuous_monitor/supervisor.py:127, continuous_monitor/**main**.py:457, submit_engine_envelope.py:356, submit_envelope_mcp.py:1192, broker_adapter/order_options.py:386, broker_adapter/retry.py:96, 7 in continuous_monitor/\*/task.py)
  * foundation: 8 sites (collector/bootstrap.py:223, collector/catchup.py:53, collector/scheduler.py:198, scheduler/emergency.py:180,209, scheduler/driver.py:139, scheduler/**main**.py:247, persistence/session.py:64)
  * distillation: 2 sites ([baselines.py:87](http://baselines.py:87) warranted; replay_harness/cli.py:316 warranted)

## Depends on

* 01a (<issue id="93254003-faaf-4c77-a6b7-920879ed87e9">ALP-455</issue>) — import-linter scaffolding (so any new exception module from this story is captured under contracts)

## Scope

In scope: triage each L4 site; narrow to specific exception class with `exc_info=exc` logging where appropriate; document warranted outermost-supervisor catches with inline comment.

### Triage pattern per site

For each L4 hit:

1. **Is this an outermost supervisor / main** entrypoint? → keep `except Exception:` but add an inline comment `# Outermost supervisor; log + keep running per runtime §G1 exception` and re-raise `CancelledError` explicitly. Examples: `__main__.py:247`, `driver.py:139`.
2. **Is this a per-iteration loop where one bad item shouldn't kill the batch?** → narrow to the actual transient class (`httpx.HTTPError`, vendor `APIError`), log with `exc_info=exc`, continue. Example: `polygon/equity.py:157` per-ticker sweep.
3. **Is this hiding a config/schema bug as a "connectivity" issue?** → narrow specifically (e.g., `yaml.YAMLError`, `pydantic.ValidationError`); let unexpected `TypeError`/`AttributeError` surface. Example: `persistence/session.py:64` reading main.yaml; `scheduler/emergency.py:180` `model_validate_json` failures.
4. **Is this** `verify_connectivity` **swallowing auth errors?** → narrow to `httpx.HTTPError | AuthError`; let other exceptions surface. Examples: `marketaux/client.py:64`, `iborrowdesk/client.py:139`, prediction-market clients.
5. **Is this** `except BaseException` **in a retry loop catching KeyboardInterrupt to write a failed-run row?** → warranted, document. Examples: `_common.py:353, 536`.

### Add structured logging

Every narrowed catch logs the exception with context:

```python
except httpx.HTTPError as exc:
    logger.warning("polygon list_aggs failed for %s", ticker, exc_info=exc)
    continue
```

For re-raises:

```python
except SomeError as exc:
    raise WrapperError(f"context") from exc
```

### Out of scope

Introducing a domain-exception hierarchy (audit suggests but doesn't mandate). Use existing exception classes or vendor classes; if a domain exception is genuinely needed, add it under the consuming module.

## Acceptance criteria

- [ ] All ~36 unwarranted L4 sites are narrowed to specific exception classes with `exc_info=exc` logging.
- [ ] ~25 warranted outermost-supervisor catches carry inline comments tying to runtime §G1 exception.
- [ ] All re-raises use `raise X from exc` for traceback chaining.
- [ ] Antipattern scanner L4 count drops to ~25 (the warranted set); the remaining hits have inline rationale.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

Re-run the antipattern scan: `uv run python .claude/skills/python-architecture/scripts/antipattern_scan.py src/alphamind | jq '.by_antipattern.L4'` — value drops from 61 to ~25. Spot-check: a synthetic `httpx.TimeoutException` raised inside `polygon/equity.py` per-ticker loop produces a log line with the ticker name and continues the sweep; an unexpected `AttributeError` does NOT get swallowed.