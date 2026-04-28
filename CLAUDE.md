# CLAUDE.md

## Linting

Run the linter after every batch of changes:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
```

Alert the user before disabling the linter or any rule in any form — including `ignore`, `per-file-ignores`, `# noqa`, and `# type: ignore`. If a subagent suppresses the linter, do not pause execution to alert user. Instead, assess whether each suppression was warranted and fix unwarranted suppressions.

## Testing

Always run the test suite parallelized via pytest-xdist:

```bash
uv run pytest -n auto
```

`-n auto` allocates one worker per CPU core. Never invoke `pytest` without `-n auto` — including from subagents and worktree verification. When verifying a narrow slice, scope to the relevant path: `uv run pytest tests/config/ -n auto`.

If a test passes serially but fails under xdist, the cause is test-order dependence (typically `sys.modules` mutation or shared filesystem state). Fix the test — do not fall back to serial.

## Spawning Subagents

- For mechanical changes, use Sonnet
- For all other changes, use Opus
- Always spawn subagents in background (async) mode
- Always list model name (Sonnet or Opus) in subagent title like this: [Sonnet | Opus] <Title>

## Your Location
If you are running on Windows, you are on the production server. 

If you are running on MacOS, you are on the development machine. Production database and logs are located here:
- Database: `/Volumes/Users/jacks/AlphaMind/data/alphamind.db`
- Logs: 
    - `/Volumes/Users/jacks/AlphaMind/logs/bootstrap.out` 
    - `/Volumes/Users/jacks/AlphaMind/logs/collector.err.log`
    - `/Volumes/Users/jacks/AlphaMind/logs/collector.log`
    - `/Volumes/Users/jacks/AlphaMind/logs/collector.out.log`
If these locations aren't accessible, alert the user