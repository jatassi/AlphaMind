# CLAUDE.md

AlphaMind: autonomous LLM swing-trading system (4–72h horizon, ~60–80 US equities),
currently in the paper-trading phase. This file is project-wide rules + a navigation
map. Process playbooks live in `docs/agents/`; per-package orientation lives in nested
`CLAUDE.md` files; design intent (point-in-time, **not** code-truth) in `docs/design/`.

## Navigation

Pipeline: **data → distillation → analysis (LLM) → decision (LLM) → execution**, with
**risk guardrails** cross-cutting. Two long-running processes share one SQLite DB: the
deliberative **pipeline** (`scheduler/`, ~3 runs/trading day) and the **continuous
monitor** (`execution/continuous_monitor/`, real-time risk on open positions).

| Layer | Code (under `src/alphamind/`) | Design intent* |
|---|---|---|
| Data | `data_sources/` `collector/` `state/` `portfolio_state/` | `docs/design/01-data-layer/` |
| Distillation | `distillation/` | `docs/design/02-distillation-layer/` |
| Analysis (LLM) | `analysis/` | `docs/design/03-analysis-layer/` |
| Decision (LLM) | `decision/` `commands/` | `docs/design/04-decision-layer/` |
| Execution | `execution/` | `docs/design/05-execution-layer/` |
| Risk (cross-cutting) | `risk_guardrails/` | `docs/design/06-risk-guardrails/` |
| Orchestration | `scheduler/` `pipeline/` | `docs/architecture/component-boundaries.md` |
| Persistence / config | `persistence/` `config/` | `docs/design/configuration-management.md` |
| Command center | `command_center/` | `docs/design/command-center.md` |
| Feedback loop | `feedback_loop/` | `docs/design/feedback-loop.md` |

\*Design docs capture original intent and rationale — treat as historical; verify
against code before relying on any specific.

Each top-level package has a `CLAUDE.md` (role, invariants, gotchas, entry point) — read
it before editing there, and append any new gotcha you hit. Import direction is
downward-only and **enforced** by `.importlinter` (the authoritative layering spec). Run
the pipeline with `python -m alphamind.scheduler`.

## Linting — run after every batch

```bash
uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports
```

Alert the operator before disabling the linter or any rule in any form (`ignore`,
`per-file-ignores`, `# noqa`, `# type: ignore`, `ignore_imports`). If a subagent
suppresses a rule, don't pause — assess each and fix the unwarranted ones. The frontend
has its own toolchain — see `command_center/frontend/CLAUDE.md`.

## Testing

CI (Windows, `-n auto`) is the authoritative full-suite gate. **Never run the full suite
locally** unless the operator explicitly asks. Scoped runs are encouraged while
implementing:

```bash
uv run pytest tests/<sub-path>/ -n auto
uv run pytest tests/path/to/test_x.py::test_case
```

Passes locally but fails under xdist on CI = test-order dependence — fix the test, don't
fall back to serial.

## Branch policy

`main` is PR-only — never push directly; open a PR. Squash-merge every PR. Don't merge
until the `ci` workflow is green. Docs/tooling-only PRs skip CI (`paths-ignore`:
`**.md`, `docs/**`, `.archive/**`, `.claude/**`, `audit-*.html`). The CI watch/merge
loop and the GitHub usage-limit fallback live in `docs/agents/ci.md`.

## Subagents

Mechanical changes → Sonnet; everything else → Opus. Always background (async) mode. Put
the model in the title: `[Sonnet|Opus] <Title>`. Subagents must commit before reporting
done — verify with `git log main..HEAD`.

## Environment

**macOS = dev machine. Windows = production server.** Production is read-only over SMB
from the dev Mac (WAL-mode SQLite; writes only run on prod):

- DB: `/Volumes/Users/jacks/AlphaMind/data/alphamind.db`
- Logs: `/Volumes/Users/jacks/AlphaMind/logs/` (`bootstrap.out`, `collector.log`, `collector.err.log`, `collector.out.log`)

Alert the operator if these aren't accessible. Scripts that hit vendor APIs need `.env`
loaded (`load_dotenv()` / `source .env`) — prod doesn't auto-source.

## Linear

Issue hierarchy + the MCP gotchas that cause silent corruption: `docs/agents/linear.md`.
