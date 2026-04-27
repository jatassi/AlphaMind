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