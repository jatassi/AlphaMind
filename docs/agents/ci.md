# CI playbook

Watching a PR's CI to green, and the GitHub usage-limit fallback. Referenced from the
root `CLAUDE.md` "Branch policy" section.

## paths-ignore and concurrency

The `ci` workflow uses `paths-ignore` for `**.md`, `docs/**`, `.archive/**`,
`.claude/**`, `audit-*.html`, and `scripts/**` — pure docs/tooling/ops-script PRs skip
the test run and can merge as soon as you open them. Any change touching `src/`,
`tests/`, `config/`, `prompts/`, `pyproject.toml`, `uv.lock`, `.importlinter`,
`alembic.ini`, or `.github/workflows/**` triggers the full CI run. (`scripts/` is
ops/investigation tooling, not production code; mypy still type-checks it on any
non-ignored PR since it walks the whole tree — only a scripts-only PR skips the gate.)

`paths-ignore` on `pull_request` evaluates the PR's **full diff**, not the latest
commit's diff. Pushing a docs-only follow-up commit to a PR that already contains code
changes still triggers a fresh CI run, because the PR's overall diff still has
non-ignored paths. The only way to "skip CI" for a follow-up is a separate docs-only PR.
`concurrency.cancel-in-progress: true` is set, so a fresh trigger cancels any in-flight
run — an unnecessary docs-only push to a busy PR throws away in-flight progress.

## Watching CI on a PR

**Do NOT invoke `gh pr checks <PR> --watch` immediately after `git push`.** GitHub takes
3–8 seconds to register the new workflow run, and `--watch` interprets the empty
pre-registration window as "no checks → exit" (status 0, "no checks reported"), so an
iterate-until-green loop falsely believes CI is done.

The reliable pattern fetches the run ID and watches it by ID — `gh run watch` blocks
until a terminal state with no pre-registration race:

```bash
# After git push, give the run a moment to register, then watch it by ID.
sleep 5
RUN_ID=$(gh run list --branch <feature-branch> --workflow ci.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status     # --exit-status returns non-zero on failure
```

`--exit-status` is critical for the loop: it propagates the run's pass/fail as the
command's exit code, so `gh run watch "$RUN_ID" --exit-status && gh pr merge ...`
short-circuits correctly on failure.

If you must use `gh pr checks --watch` (e.g. multiple workflows gate the PR), guard the
pre-registration race:

```bash
until [ "$(gh pr checks <PR> --json status -q 'length' 2>/dev/null)" -gt 0 ]; do sleep 2; done
gh pr checks <PR> --watch
```

On a failed run, fetch logs via `gh run view <RUN_ID> --log-failed --job <JOB_ID>` (the
failed-job ID is printed by `gh run watch`). `--log-failed` filters to just the failing
job's output.

## GitHub usage-limit fallback

GitHub disables Actions when the account's billing fails or the spending limit is hit.
Then **every job reports `conclusion: failure` with an empty `steps: []` array** (jobs
never started) and `gh run view <RUN_ID>` prints the literal sentinel:

```
The job was not started because recent account payments have failed or your spending limit needs to be increased. Please check the 'Billing & plans' section in your settings
```

Detect it programmatically before falling back:

```bash
gh run view <RUN_ID> 2>&1 | grep -q "The job was not started because recent account payments have failed or your spending limit needs to be increased" \
  && echo "USAGE LIMIT — fall back to local CI" \
  || echo "Real CI failure — debug normally"
```

If — and only if — the sentinel is present, GitHub CI is unavailable as the gate. Run
the full CI chain locally, mirroring every step in `.github/workflows/ci.yml`:

```bash
# Python lint job (Linux in CI)
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run lint-imports

# Python test job (Windows in CI) — single-threaded locally
uv run pytest                 # NO -n auto — CI runs `-n auto`; local must NOT

# Frontend job — ONLY if the PR's diff touches src/alphamind/command_center/frontend/**
cd src/alphamind/command_center/frontend
bun install --frozen-lockfile
bun run lint
bun run format:check
bun run typecheck
bun run test
cd -
```

Iterate until every step is green locally, then merge bypassing GitHub CI (e.g.
`gh pr merge --squash --admin <PR>`). This fallback applies **only** when the sentinel is
present — for any other failure (real test failure, infra flake, runner timeout), fix
the issue and let CI re-run.
