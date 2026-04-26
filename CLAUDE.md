# CLAUDE.md

## Linting

Run the linter after every batch of changes:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
```

Alert the user before disabling the linter or any rule in any form — including `ignore`, `per-file-ignores`, `# noqa`, and `# type: ignore`.

## Spawning Subagents

- For mechanical changes, use Sonnet
- For all other changes, use Opus
- Always spawn subagents asynchronously
- Always list model name (Sonnet or Opus) in subagent title

