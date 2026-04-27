---
status: done
completed_date: 2026-04-26
commit_id: 667396e
---

# 07 — NSSM service registration

## Goal

Register the `alphamind-collector` Windows service via NSSM, with restart-on-failure, automatic-delayed start, and stdout/stderr capture per `infrastructure.md`. Provide install/uninstall scripts and a one-page operator runbook.

## Reading

- `docs/architecture/infrastructure.md` § Process supervision — NSSM configuration pattern
- `docs/design/01-data-layer/collector/runner.md` § Process supervision — collector-specific config

## Depends on

- 06b (the `python -m alphamind.collector run` command must exist)

## Scope

In scope:
- `scripts/install_collector_service.ps1` — PowerShell script that:
  - Resolves the Python executable inside the project's `uv` environment.
  - Calls `nssm install alphamind-collector ...` pointing at `python -m alphamind.collector run` with the project root as `AppDirectory`.
  - Sets `AppExit Default Restart` and a 60s throttle.
  - Sets `Start = Automatic (Delayed Start)`.
  - Redirects `AppStdout` and `AppStderr` to `%USERPROFILE%\AlphaMind\logs\collector.out.log` and `collector.err.log`.
  - Runs the service as the operator's user account (not LocalSystem).
- `scripts/uninstall_collector_service.ps1` — counterpart that calls `nssm remove alphamind-collector confirm` and prints next-steps.
- `docs/implementation/01-data-layer/collector/RUNBOOK.md` (new) — one-page operator runbook covering:
  - Prerequisites (NSSM installed; `.env` populated; bootstrap completed).
  - `nssm start alphamind-collector` / `nssm stop alphamind-collector` / `nssm restart alphamind-collector`.
  - Where to find logs: `%USERPROFILE%\AlphaMind\logs\collector.{log,out.log,err.log}`.
  - How to apply config edits (restart the service).
  - The revoked-key drill from `lifecycle.md` § Operator workflow.

Out of scope:
- NSSM installation (operator concern; NSSM is a separate download).
- End-to-end verification (story 08).
- macOS / Linux equivalents (paper trading is Windows-only per `infrastructure.md`).

## Notes

NSSM commands the install script needs:

```
nssm install alphamind-collector "<python>" "-m" "alphamind.collector" "run"
nssm set alphamind-collector AppDirectory "<repo_root>"
nssm set alphamind-collector AppExit Default Restart
nssm set alphamind-collector AppRestartDelay 60000
nssm set alphamind-collector Start SERVICE_DELAYED_AUTO_START
nssm set alphamind-collector AppStdout "%USERPROFILE%\AlphaMind\logs\collector.out.log"
nssm set alphamind-collector AppStderr "%USERPROFILE%\AlphaMind\logs\collector.err.log"
nssm set alphamind-collector ObjectName <DOMAIN>\<USER> <PASSWORD>
```

The operator-account password is the trickiest part — the install script should prompt for it interactively rather than accepting it as a parameter (avoids leaving it in shell history). Acceptable alternative: leave `ObjectName` configuration to a manual final step in the runbook.

The Python executable path: `uv` projects expose the venv at `.venv/Scripts/python.exe` after `uv sync`. The install script should resolve this and pass the absolute path to NSSM.

`AppDirectory` is the project root so relative paths in config files resolve correctly.

## Acceptance criteria

- [ ] `scripts/install_collector_service.ps1` registers `alphamind-collector` with NSSM using `python -m alphamind.collector run` as the entry point.
- [ ] `AppExit Default Restart` and 60s throttle configured.
- [ ] Start type is `Automatic (Delayed Start)`.
- [ ] Stdout / stderr redirected to the logs directory.
- [ ] Service runs as the operator's user account (configured by the script or documented for manual finalization).
- [ ] `scripts/uninstall_collector_service.ps1` removes the service cleanly.
- [ ] `RUNBOOK.md` documents start/stop/restart, log locations, config-reload procedure, and the revoked-key drill.
- [ ] After `install_collector_service.ps1` runs and the service starts, `Get-Service alphamind-collector` reports `Running`.
- [ ] After service stop, `Get-Service alphamind-collector` reports `Stopped` and the most recent log lines are flushed to disk.
- [ ] `nssm restart alphamind-collector` cleanly cycles the service and the new process picks up edited config files.
