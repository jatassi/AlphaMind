# 07 — Verify + RUNBOOK + NSSM

## Goal

Closeout story: ship the end-to-end verify script that boots all three daemons (pipeline + monitor + command center), exercises a passkey registration + login roundtrip, hits each of the 8 control verbs, observes at least one alert firing, and exits non-zero on any failure. Plus the operator RUNBOOK documenting bring-up + tear-down + recovery + troubleshooting, and the NSSM service-install scripts for the command center process (mirroring pipeline + monitor patterns). Per `/draft-user-stories` Phase 4, command center is an out-of-pipeline exception → dedicated verify + runbook. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

## Reading

* `scripts/verify_debug_e2e.py` — canonical structure for an e2e verify script (process boot, subprocess management, assertion patterns).
* `scripts/RUNBOOK_end_to_end_verification.md` — canonical structure for a runbook.
* `scripts/install_pipeline_service.ps1` / `install_monitor_service.ps1` (or equivalent NSSM scripts) — pattern to mirror for the command center service.
* `docs/design/command-center.md` § Architecture context, § Tech stack, § Authentication and access — operator-facing behavior the runbook documents.
* All preceding stories' verify entry points — the verify script exercises each.

## Depends on

* [ALP-663](<https://linear.app/alphamind-jatassi/issue/ALP-663>) (01a) — PROFILE_SWITCHED substrate; exercised by the switch_profile verb test.
* [ALP-664](<https://linear.app/alphamind-jatassi/issue/ALP-664>) (01b) — pipeline HTTP+SSE; exercised by 5 pipeline verbs.
* [ALP-665](<https://linear.app/alphamind-jatassi/issue/ALP-665>) (01c) — monitor HTTP+SSE; exercised by 3 monitor verbs.
* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — backend foundation; verifies boot.
* [ALP-667](<https://linear.app/alphamind-jatassi/issue/ALP-667>) (03) — auth; passkey roundtrip.
* [ALP-668](<https://linear.app/alphamind-jatassi/issue/ALP-668>) (04a) — control proxy; exercises all 8 verbs.
* [ALP-669](<https://linear.app/alphamind-jatassi/issue/ALP-669>) (04b) — SSE multiplexer; verifies events flow.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation; verifies `bun run build` + StaticFiles mount.
* [ALP-671](<https://linear.app/alphamind-jatassi/issue/ALP-671>) (05a) — alerts; verifies one rule fires end-to-end.

(View stories 05b–05j and 06a–06c are NOT blockers — verify is backend smoke + service install, not a browser-level UI test per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (K).)

## Scope

`scripts/verify_command_center.py`, `scripts/RUNBOOK_command_center.md`, `scripts/install_command_center_service.ps1` (NSSM). Tests at `tests/scripts/test_verify_command_center.py` (verifies the script's check structure).

### 1\. `scripts/verify_command_center.py`

A standalone Python script (CLI) that:

1. **Boots all three daemons.** Subprocess: `python -m alphamind.scheduler --debug-e2e`, `python -m alphamind.execution.continuous_monitor --synthetic`, `python -m alphamind.command_center`. Waits up to 30 s for each to bind their port. Fails the script on boot timeout.
2. **Passkey roundtrip.** Drives `/auth/register/begin` → `/auth/register/complete` → `/auth/login/begin` → `/auth/login/complete` using the FakeWebauthnVerifier from story 03 (the verify script flips a config flag enabling the fake verifier — production never sees it). Asserts session cookie + CSRF cookie are set.
3. **Eight control verbs.** Hits `/api/control/pause`, `/resume`, `/trigger_emergency_invocation`, `/switch_profile`, `/run_universe_validation`, `/cancel_order`, `/force_close_position`, `/set_halt_mode`. For each, asserts a successful response envelope + corresponding `activity_log` row with `source=operator_console`. For `switch_profile`, additionally asserts a `PROFILE_SWITCHED` activity_log row.
4. **One alert fires.** Forces a condition that triggers one of the 17 default rules (the simplest: pause then resume → no rule; better: trigger an emergency invocation, which is the `EMERGENCY_INVOCATION_REQUESTED` event the "emergency invocation requested" rule consumes if present; otherwise use a synthetic breach scenario the monitor's --synthetic mode can produce). Asserts: `alerts` row written, Discord channel was called (fake), in-app SSE event emitted.
5. **SSE roundtrip.** Subscribes to `/api/events`; observes at least one pipeline event + one monitor event arriving; verifies dual `<source>:<event_name>` framing.
6. **Frontend build.** Runs `bun install --frozen-lockfile && bun run build` in `src/alphamind/command_center/frontend/`; asserts `dist/index.html` exists; spot-checks the FastAPI StaticFiles mount returns it.
7. **Tears down.** SIGTERMs each daemon; asserts clean shutdown within 10 s each.

Report: prints a per-step PASS / FAIL table; exits 0 only if all steps pass.

### 2\. `scripts/RUNBOOK_command_center.md`

Operator runbook covering:

* **Initial bring-up** — first-launch setup-token retrieval; passkey registration (real authenticator, not the fake); DB initialization (Alembic upgrade); bun install + `bun run build` for the frontend; NSSM service install. Document the bun-installation precondition on the trading machine (operator runs `curl -fsSL https://bun.sh/install | bash` or the equivalent Windows installer once before first bring-up).
* **Day-to-day operations** — accessing the UI (loopback URL, LAN URL); restart procedure; log file locations.
* **Adding a passkey** — re-registration flow when adding a new device.
* **Troubleshooting** — common failure modes: bind-port conflict, foreign-table writer rejection (DB access split), pipeline / monitor loopback unreachable (consumer reconnect logs), WebAuthn rp-id mismatch (browser cert errors), `alerts.yaml` reload failures, bun version mismatch between dev / CI / trading machine.
* **Recovery** — DB corruption recovery (restore from snapshot); credential reset (manual SQL delete from `webauthn_credentials` + re-bootstrap setup token).
* **Operator-handover note** — VPS Caddy + WireGuard remote-access wiring deferred per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (B); operator runs that handover separately when ready.

### 3\. `scripts/install_command_center_service.ps1`

PowerShell script using NSSM to install the command center as a Windows service:

* Service name: `AlphaMindCommandCenter`.
* Executable: `python -m alphamind.command_center`.
* Working dir: repo root.
* Stdout / stderr capture to `%USERPROFILE%/AlphaMind/logs/command_center.{out,err}.log`.
* Restart policy: on-failure.
* Dependencies: AlphaMindPipeline + AlphaMindMonitor (the command center is the third service per the design's architecture context).

### Out of scope

* Browser-level e2e tests (Playwright / Cypress) — Vitest unit tests at component level handle frontend; the verify script's frontend check is just `bun run build` + StaticFiles probe.
* VPS-side artifacts (WG peer entry, Caddy config) — operator-maintained outside this repo per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (B).

## Acceptance criteria

- [ ] `scripts/verify_command_center.py` exists; running it against a clean dev machine boots the three daemons, drives the 7 numbered checks, and exits 0 on full pass.
- [ ] The verify script exits non-zero on any check failure with a per-step pass/fail report.
- [ ] `scripts/RUNBOOK_command_center.md` covers initial bring-up, day-to-day, passkey-add, troubleshooting (≥6 named failure modes including bun version mismatch), recovery, operator-handover deferral note, and explicit bun-installation precondition step.
- [ ] `scripts/install_command_center_service.ps1` installs the service via NSSM with documented configuration.
- [ ] `tests/scripts/test_verify_command_center.py` asserts the script's check structure exists (e.g., parses the script to verify all 7 checks are present + each has assertion logic).
- [ ] No browser-level e2e tests added (per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (K)).
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Local: `uv run python scripts/verify_command_center.py` on a dev machine completes with exit 0. CI: the `tests/scripts/test_verify_command_center.py` shape-check runs in the normal pytest matrix.