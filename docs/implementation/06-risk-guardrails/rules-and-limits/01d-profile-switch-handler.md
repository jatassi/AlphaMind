---
status: not_started
completed_date:
commit_id:
---

# 01d — Profile switch handler

## Goal

Land the core handler that backs the `POST /control/switch_profile` operator action: validate that the requested profile name corresponds to an existing `config/profiles/{name}.yaml` file, atomically rewrite `main.yaml`'s `active_profile` field (preserving every other field and YAML formatting where reasonable), and return a structured outcome carrying the previous and new profile labels. The change "takes effect at next invocation" per `pipeline-control-and-events-schema.md` — the scheduler re-reads the YAML tree before each invocation, so the handler's job is purely the file edit, with no in-process hot-reload.

This story ships the *handler function*. Wiring it into the pipeline's `/control/*` HTTP surface is owned by the pipeline-control implementation work (not yet drafted); wiring the activity-log emission is owned by the activity-log + state-persistence machinery (also forthcoming). This story exposes the function and the structured outcome, and ships an `activity_log_entry` factory that future callers compose into their persistence layer.

## Reading

- `docs/design/06-risk-guardrails/rules-and-limits.md` § Transitioning between profiles — manual transition, operator review of expanded feature set, no automatic downgrade
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Sequential validation lifecycle — graduation action is "the operator's edit of `main.yaml`'s `active_profile` (or use of `POST /control/switch_profile`)"
- `docs/design/pipeline-control-and-events-schema.md` § `switch_profile_request` and § Per-verb summary — the request schema (`{profile_name: string}`), the response (`control_response_envelope`), and the error codes (`not_found` 404 when the file is missing, `validation_failed` 400)
- `docs/design/command-center.md` § `POST /control/switch_profile` — the operator-facing description ("Writes `main.yaml`'s `active_profile`; takes effect at next invocation")
- `docs/design/configuration-management.md` § Composition model — the operator-pinned dimension of the cascade (the profile is what the handler edits)
- `docs/design/configuration-management.md` § Reload model — confirms the scheduler re-reads YAML before each invocation; no hot-reload path needed
- `src/alphamind/config/models/main.py` — `MainConfig`, `Profile` enum, `ExecutionMode`, `Paths`
- `src/alphamind/config/loaders.py` — `load_profiles`, `read_yaml_file` (the helpers that already validate profile-file presence)
- `config/main.yaml` — the file being edited
- `config/profiles/{micro,small,medium,large}.yaml` — the four valid targets

## Depends on

None. The configuration-management feature has shipped `Profile`, `MainConfig`, and the loader helpers.

## Scope

In scope:

- `src/alphamind/risk_guardrails/rules_and_limits/profile_switch.py` defining:
  - `ProfileSwitchOutcome` (frozen, slots dataclass): `previous_profile: Profile`, `new_profile: Profile`, `main_yaml_path: Path`, `is_no_op: bool`. `is_no_op == True` when the new profile equals the previous; the file is left unchanged in that case (no rewrite, no fsync), but the outcome dataclass is still returned so callers can record the operator action without inferring no-op-ness.
  - `ProfileNotFoundError(LookupError)` — raised when the requested profile name does not match a file present under `config/profiles/`. Subclassing `LookupError` matches the standard-library convention for "named thing not found"; the upstream HTTP layer maps it to the `not_found` (404) error code per `pipeline-control-and-events-schema.md`.
  - `switch_active_profile(*, new_profile: Profile, config_dir: Path) -> ProfileSwitchOutcome` — the handler. Steps:
    1. Resolve `main_yaml_path = config_dir / "main.yaml"`. Read it via `read_yaml_file` and parse via `MainConfig.model_validate`. The current `MainConfig.active_profile` is the `previous_profile`.
    2. Verify the target file exists at `config_dir / "profiles" / f"{new_profile.value}.yaml"`. Raise `ProfileNotFoundError` (with the missing path in the message) if not.
    3. If `previous_profile == new_profile`: return a `ProfileSwitchOutcome` with `is_no_op=True`, no file write.
    4. Otherwise: rewrite `main.yaml` with the new `active_profile`. Use the atomic write pattern documented in story 07 of foundation/configuration (`tmp file → fsync → rename`); preserve all non-`active_profile` fields by round-tripping through `ruamel.yaml` (which preserves comments and formatting) **if** the dependency is already in `pyproject.toml`, else fall back to PyYAML round-trip with the explicit caveat that comments are lost. Return a `ProfileSwitchOutcome` with `is_no_op=False`.
  - `build_profile_switch_activity_log_entry(outcome: ProfileSwitchOutcome, *, source: str = "operator_console") -> dict[str, Any]` — a small factory that returns the activity-log entry payload the persistence layer eventually writes. Shape:
    ```python
    {
        "event_type": "profile_switched",
        "source": source,
        "previous_profile": outcome.previous_profile.value,
        "new_profile": outcome.new_profile.value,
        "is_no_op": outcome.is_no_op,
    }
    ```
    The factory is co-shipped with the handler so callers don't reinvent the schema. Persistence-layer integration (writing this to the `activity_log` table) is out of scope.
- `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports `ProfileSwitchOutcome`, `ProfileNotFoundError`, `switch_active_profile`, `build_profile_switch_activity_log_entry`. Combine with sibling stories' re-exports via the natural union merge.
- Unit tests covering:
  - **Happy path.** Construct a temporary `config/` tree (copy the shipped tree to `tmp_path`); call `switch_active_profile(new_profile=Profile.large, config_dir=tmp_path)`; assert the returned outcome has `previous_profile=Profile.medium` (matching the shipped `main.yaml`) and `new_profile=Profile.large` and `is_no_op=False`; reload `main.yaml` from disk and assert `MainConfig.active_profile == Profile.large`; assert non-`active_profile` fields (`execution_mode`, `paths`) are unchanged.
  - **No-op.** Same setup; call with `new_profile=Profile.medium` (the current); assert `is_no_op=True`; assert the file's mtime is unchanged (or the contents are byte-identical to the pre-call snapshot).
  - **Profile not found.** Construct a temporary `config/` tree; delete `config/profiles/large.yaml`; call `switch_active_profile(new_profile=Profile.large, config_dir=tmp_path)`; assert `ProfileNotFoundError` is raised with a message naming `large.yaml` (or the missing path).
  - **Atomic write semantics.** Either via mock-injected I/O failures or by inspecting the implementation, assert that a partial write does not corrupt `main.yaml`. Easiest test: mock `os.replace` (or the equivalent atomic-rename call) to fail; assert `main.yaml` is byte-identical to its pre-call state.
  - **Activity-log entry shape.** Assert `build_profile_switch_activity_log_entry(...)` returns a dict matching the documented shape with the expected keys and values.
  - **Custom `source` field.** Assert that passing `source="api"` (or any non-default string) is reflected in the returned entry.

Out of scope:

- The HTTP `/control/switch_profile` endpoint itself — owned by pipeline-control implementation work.
- Writing the activity-log entry to the SQLite `activity_log` table — owned by the activity-log persistence machinery.
- Hot-reloading the running pipeline — explicitly *not* in the design (the change takes effect at next invocation).
- Validating the *new* profile's contents (e.g., that its YAML still parses) — the cross-reference and semantic-self-test validators already gate any malformed profile at the next invocation's load. The handler's contract is "swap the pointer," not "verify the destination."
- Operator confirmation flow (the `/feedback-review` walkthrough described in `rules-and-limits.md § Operator confirmation`) — that is operator workflow, not a code path.
- Authentication or authorization — owned by the command-center's WebAuthn layer.

## Notes

**`ruamel.yaml` vs. PyYAML for the rewrite.** `ruamel.yaml` round-trips comments and preserves formatting; PyYAML's `safe_dump` rewrites the file from a parsed dict and loses both. The shipped `main.yaml` carries comments documenting the path expansions and execution-mode choices, which the operator should not lose on a profile switch. **Check whether `ruamel.yaml` is in `pyproject.toml`'s dependencies.** If yes, use it. If no, the simplest path is a *targeted* rewrite — read the file as text, use a regex to locate the `active_profile:` line, and replace just the value, leaving every other byte untouched. The targeted-rewrite approach is more brittle than `ruamel.yaml` but does not introduce a new dependency. Surface to the orchestrator if the dependency is absent and you'd prefer a different approach.

**Atomic write pattern.** Match the implementation in `src/alphamind/config/snapshot.py` (story 07): write to a sibling temp file, `fsync` the file descriptor, `os.replace` to the final name, `fsync` the parent directory. This ensures a power loss mid-write leaves the original `main.yaml` intact.

**No-op semantics.** A switch from `medium` to `medium` is a legitimate operator action (e.g., "I'm confirming the current profile is still right"). Returning a `ProfileSwitchOutcome` with `is_no_op=True` lets the caller record the operator's intent in the activity log without writing the file. The activity-log entry factory honors this — `is_no_op=True` is preserved in the entry payload, so the operator's "I checked" is durable.

**`ProfileNotFoundError` subclasses `LookupError`.** The pipeline-control HTTP layer maps Python exceptions to error codes; `LookupError`-style "missing key" surfaces map naturally to HTTP 404. Avoid `FileNotFoundError` (which is a subclass of `OSError` and may be conflated with permission/IO issues); the missing profile is a *named* lookup failure, not an OS failure.

**Why the activity-log factory ships here, not in the activity-log machinery.** The factory's shape is determined by what *this* handler produces (previous, new, no-op-ness), not by the activity-log table's schema. Co-shipping the factory with the handler makes the contract obvious; the activity-log machinery's contribution is the table column shapes and the persistence call, both of which consume this factory's output.

**`config_dir` parameter, not `paths.archive` or anything else.** The handler edits `main.yaml`, which lives at `config_dir / "main.yaml"`. The `Paths` dataclass on `MainConfig` documents pipeline runtime paths (database, logs, archive); they are not relevant to the handler. Passing `config_dir` explicitly mirrors the loader functions' signatures.

**Profile enum, not raw string.** The function signature accepts `new_profile: Profile`. The HTTP layer parses the request body's `profile_name` string against the `Profile` enum (raising `ValidationError` on an unknown name, mapping to HTTP 400 `validation_failed`). By the time the handler runs, the input is enum-typed; `ProfileNotFoundError` covers the *file-missing* case (a known-enum-member with no shipped YAML — e.g., a partially-deleted config tree), which the schema validation cannot catch.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/rules_and_limits/profile_switch.py` exists and defines `ProfileSwitchOutcome`, `ProfileNotFoundError`, `switch_active_profile`, `build_profile_switch_activity_log_entry`.
- [ ] `ProfileSwitchOutcome` is a `dataclass(frozen=True, slots=True)`.
- [ ] `ProfileNotFoundError` is a subclass of `LookupError`.
- [ ] `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports the four names.
- [ ] A unit test asserts a happy-path call rewrites `main.yaml`'s `active_profile` from `medium` to `large`, preserving `execution_mode` and `paths`, and returns `previous_profile=medium`, `new_profile=large`, `is_no_op=False`.
- [ ] A unit test asserts the no-op case (current profile passed in) returns `is_no_op=True` and leaves the file byte-identical.
- [ ] A unit test asserts `ProfileNotFoundError` is raised when the target profile's YAML file is absent under `config/profiles/`, with a message naming the missing path.
- [ ] A unit test asserts atomic-write semantics — a simulated failure mid-rewrite leaves `main.yaml` byte-identical to its pre-call state.
- [ ] A unit test asserts `build_profile_switch_activity_log_entry` returns a dict carrying `event_type=profile_switched`, `source` (default `operator_console` or the override), and the previous/new profile values.
- [ ] A unit test asserts the activity-log entry payload accepts an explicit `source="api"` and reflects it in the returned dict.
- [ ] A unit test asserts non-`active_profile` fields in `main.yaml` are unchanged after the rewrite (`execution_mode`, every `paths.*` value).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
