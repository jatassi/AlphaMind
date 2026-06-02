# Dispatching the Grok CLI for mechanical subagent work

Referenced from the root `CLAUDE.md` "Subagents" section.

The Grok CLI (`grok`, `~/.grok/bin/grok`) is a headless coding agent that can run
mechanical changes **off the Claude usage budget** — which is shared by mainline dev and
live AlphaMind operations (pipeline + continuous monitor). Use it to conserve that budget;
keep judgment work on Claude.

## When to use it — and when not

- **Use for mechanical, well-specified changes:** verbatim refactors, fixture/builder
  hoists, dedup, renames, boilerplate, single-file additive edits. On a head-to-head eval
  of three test-dedup stories it matched or beat Sonnet on output completeness for pure
  mechanical hoists, and was faster wall-clock.
- **Do NOT use for judgment-heavy work:** deciding what to keep vs delete, design,
  anything where preserving behavioral coverage or nuanced correctness matters. In the eval
  grok-build consistently **over-deleted** real behavioral tests on the one story that
  required telling "echo" from "behavior" — it filtered on coverage-parity alone and missed
  assertions line-coverage can't see. Dispatch those to Opus/Sonnet.

## Model

Pass `-m grok-build` (xAI's "Grok 4.3", *"best for advanced coding tasks"*). The CLI
**default `grok-composer-2.5-fast` is Cursor's Composer model, not Grok** — don't use it
when you mean to run Grok. Neither model supports `--effort` (no reasoning-effort knob).

## Invocation (headless, autonomous, isolated)

```bash
# create an isolated worktree for the agent to edit (it works in its cwd)
git worktree add --detach .claude/worktrees/grok-<task> <base-sha>

cd .claude/worktrees/grok-<task> && grok -m grok-build \
  --prompt-file <prompt.txt> \   # long prompts; -p "<text>" for short ones
  --always-approve \             # autonomous tool use (also the user's config default)
  --no-subagents \               # no fan-out (parity with a Claude subagent)
  --max-turns 500 \              # see gotcha below — 200 is too low for multi-file edits
  --output-format plain          # final response to stdout; intermediate steps NOT streamed
```

- It auto-discovers the repo `CLAUDE.md`, so it inherits project conventions.
- **`cd` must be in the same command** (shell cwd does not persist between separate tool
  calls). Otherwise grok runs in the main checkout and edits the wrong tree. `--cwd <abs>`
  works too.
- Embed the full task spec in the prompt (don't rely on Linear/MCP access being symmetric).

## Operational gotchas (learned the hard way)

- **One tool-call per turn, many small `search_replace` edits.** A multi-file refactor
  easily exhausts a low `--max-turns` and **stops without committing**. Use `--max-turns
  500`. A *truncated* grok looks like broken output (e.g. it never reached the pytest/verify
  step, so tests are left failing) — distinguish truncation from a real defect before judging.
- **Token-per-minute rate limit (team 7.5M TPM).** Three concurrent grok-build jobs trip
  HTTP 429 and truncate. **Dispatch grok jobs sequentially (≤2 at a time)** — the opposite of
  how you parallelize Claude subagents.
- **`search_replace` is flaky on large/repetitive files** ("string to replace not found");
  it recovers when it has turns to spare.
- **It commits last.** A cut-off run leaves work uncommitted in the worktree.
- The `worker quit ... Auth(AuthorizationRequired)` log line is **benign** (the disabled
  secondary-model fork), not an auth failure.

## Always verify the output yourself

grok self-reports success even when truncated — do not trust the report. For a mechanical
refactor: run the scoped `pytest` (per `CLAUDE.md` "Testing"), diff for unintended `src/`
changes, confirm no behavioral test was dropped, run the lint chain, and check it actually
committed (`git -C <worktree> log`). Then integrate as you would any subagent's branch.

## Auth

`grok login` (cached in `~/.grok/auth.json`). `grok models` may print "not authenticated"
even when auth works — ignore it; smoke-test with `grok -p "say PONG" --max-turns 1`.
