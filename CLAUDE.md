# AlphaMind Agent Documentation

AlphaMind: autonomous LLM swing-trading system (4–72h horizon, ~60–80 US equities),
currently in the paper-trading phase. This file is project-wide rules + a navigation
map. Process playbooks live in `docs/agents/`; per-package orientation lives in nested
`CLAUDE.md` files; design intent (point-in-time, **not** code-truth) in `docs/design/`.

## Navigation

Pipeline: **data → distillation → analysis (LLM) → decision (LLM) → execution**, with
**risk guardrails** cross-cutting. Two long-running processes share one SQLite DB: the
deliberative **pipeline** (`scheduler/`, ~3 runs/trading day) and the **continuous
monitor** (`execution/continuous_monitor/`, real-time risk on open positions).

| Layer | Code (`src/alphamind/`) | 
|---|---|
| Data | `data_sources/` `collector/` `state/` `portfolio_state/` |
| Distillation | `distillation/` |
| Analysis (LLM) | `analysis/` |
| Decision (LLM) | `decision/` `commands/` |
| Execution | `execution/` |
| Risk (cross-cutting) | `risk_guardrails/` |
| Orchestration | `scheduler/` `pipeline/` |
| Persistence / config | `persistence/` `config/` |
| Command center | `command_center/` |
| Feedback loop | `feedback_loop/` |

Each top-level package has a `CLAUDE.md` — read it before editing there, and append any new gotcha you hit. Opaque/large packages also carry a generated `Modules`
table; grep `Concerns:` across the nested files to find which package owns a
cross-cutting behavior. Import direction is downward-only and **enforced** by `.importlinter` (the authoritative layering spec). Run the pipeline with `python -m alphamind.scheduler`.

## Linting — run after every batch

```bash
uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports
```

Alert the user before disabling the linter or any rule in any form (`ignore`,
`per-file-ignores`, `# noqa`, `# type: ignore`, `ignore_imports`). If a subagent
suppresses a rule, don't pause — assess each and fix the unwarranted ones. The frontend
has its own toolchain — see `command_center/frontend/CLAUDE.md`.

## Testing

CI (Windows, `-n auto`) is the authoritative full-suite gate. **Never run the full suite
locally** unless the user explicitly asks. Scoped runs are encouraged while
implementing:

```bash
uv run pytest tests/<sub-path>/ -n auto
uv run pytest tests/path/to/test_x.py::test_case
```

Passes locally but fails under xdist on CI = test-order dependence — fix the test, don't
fall back to serial.

### Test-quality rules

**Mock only at the four sanctioned boundaries:** LLM / Claude Agent SDK, broker API
(Alpaca), the system clock, and the database. Patching an internal collaborator (a
runner, assembler, composition-root factory) tests wiring, not behavior — disallowed.

**Coverage is a floor, not a target.** Before adding a test ask: "does this test fail
for a reason no other test fails for?" If no, it is redundant — don't add it.

**Red-green-refactor's third step includes the tests.** After driving out an
implementation test-first, consolidate the new tests against each other and against the
integration tests covering the same path; delete scaffolding the coarser test subsumes.

**Architecture / purity invariants belong in `.importlinter` contracts or ruff rules**
— never in drifting-count "audit-baseline ceiling" tests or source-text AST greps.

See `docs/agents/testing.md` for rationale and before/after examples.

## Branch policy

`main` is PR-only — never push directly; open a PR. Squash-merge every PR. Don't merge
until the `ci` workflow is green. Docs/tooling-only PRs skip CI (`paths-ignore`:
`**.md`, `docs/**`, `.archive/**`, `.claude/**`, `audit-*.html`, `scripts/**`). The CI
watch/merge loop and the GitHub usage-limit fallback live in `docs/agents/ci.md`.

## Subagents

Pick by how much the task leaves to decide:

- **Opus** — the approach itself must be worked out: design, architectural/algorithmic/schema
  decisions, ambiguous or underspecified specs, non-obvious trade-offs.
- **Sonnet** — the approach is clear, but correct execution still needs discretion a green
  test/lint run wouldn't prove on its own: preserving existing behavior, choosing what to keep
  vs cut within a known pattern, cross-file coherence, edge cases.
- **Grok** (via its CLI) — a rote, fully-specified change whose correctness is captured
  entirely by an objective gate (tests + lint), with nothing left to decide: mechanical
  renames, verbatim refactors, applying a specified transform across files. Read
  `docs/agents/grok-cli.md` before dispatching.

Always background (async) mode. Put the model in the title: `[Sonnet|Opus|Grok] <Title>`.
Subagents must commit before reporting done — verify with `git log main..HEAD`.

## Environment

**macOS = dev machine. Windows = production server.** Production is read-only over SMB
from the dev Mac (WAL-mode SQLite; writes only run on prod):

- DB: `/Volumes/Users/jacks/AlphaMind/data/alphamind.db`
- Logs: `/Volumes/Users/jacks/AlphaMind/logs/` (`bootstrap.out`, `collector.log`, `collector.err.log`, `collector.out.log`)

Alert the user if these aren't accessible. Scripts that hit vendor APIs need `.env`
loaded (`load_dotenv()` / `source .env`) — prod doesn't auto-source.

## Linear

Read `docs/agents/linear.md` before using Linear tools.
