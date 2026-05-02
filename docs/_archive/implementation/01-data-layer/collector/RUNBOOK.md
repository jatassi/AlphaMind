# alphamind-collector Operator Runbook

One-page reference for day-to-day operation of the `alphamind-collector` Windows service.

---

## Prerequisites

Before installing the service:

1. **NSSM installed and on PATH** — download from https://nssm.cc/ and place the binary in a directory on `%PATH%`.
2. **`.env` populated** — copy `.env.example` to `.env` at the project root and fill in all API keys. See `docs/design/01-data-layer/api-key-checklist.md` for the full key inventory.
3. **Bootstrap completed** — run the one-time historical backfill:
   ```powershell
   python -m alphamind.collector bootstrap
   ```
   Expected runtime: 30–60 minutes. Watch `%USERPROFILE%\AlphaMind\logs\collector.log` for progress. Spot-check row counts against the volume table in `docs/design/01-data-layer/collector/lifecycle.md § Bootstrap`.
4. **`uv sync` run** — the install script resolves `.venv\Scripts\python.exe`; the venv must exist before registering the service.

---

## Install the service

Run from an elevated PowerShell session (Run as Administrator):

```powershell
.\scripts\install_collector_service.ps1
```

After the script completes, configure the service account so that `%USERPROFILE%` resolves to the operator's profile and outbound network credentials are inherited:

```powershell
nssm set alphamind-collector ObjectName .\YourUsername
```

NSSM will prompt for the password interactively. For a domain account use `DOMAIN\YourUsername`.

Then start the service:

```powershell
nssm start alphamind-collector
Get-Service alphamind-collector   # should report: Running
```

---

## Start, stop, and restart

| Action | Command |
|--------|---------|
| Start | `nssm start alphamind-collector` |
| Stop | `nssm stop alphamind-collector` |
| Restart | `nssm restart alphamind-collector` |
| Status | `Get-Service alphamind-collector` |

Windows Service Control Manager equivalents (`sc start alphamind-collector`, `services.msc`) also work once the service is registered.

After a stop, `Get-Service alphamind-collector` reports `Stopped` and log files are flushed to disk.

---

## Log locations

| File | Contents |
|------|----------|
| `%USERPROFILE%\AlphaMind\logs\collector.log` | Python `logging` output — scheduler events, collection-run summaries, errors |
| `%USERPROFILE%\AlphaMind\logs\collector.out.log` | Process stdout captured by NSSM |
| `%USERPROFILE%\AlphaMind\logs\collector.err.log` | Process stderr captured by NSSM |

Logs rotate daily; 30-day retention. For verbose per-call output set `LOG_LEVEL=DEBUG` in `.env` and restart the service.

To tail live output:

```powershell
Get-Content "$env:USERPROFILE\AlphaMind\logs\collector.log" -Wait -Tail 50
```

---

## Applying configuration changes

Configuration files read at startup:

| File | Owns |
|------|------|
| `config/collector_schedule.yaml` | Per-collector cron expressions |
| `config/data_sources.yaml` | Per-provider rate limits, retry shapes, secret env-var references |
| `config/assets.yaml` | Trading universe and benchmark tickers |
| `.env` | API keys and secrets |

APScheduler does not reload mid-run. After editing any configuration file, restart the service to pick up changes:

```powershell
nssm restart alphamind-collector
```

Verify the new schedule took effect by watching `collector.log` for the next scheduled fire.

---

## Revoked-key drill (go-live check)

Run this check before leaving the system unattended for the first time:

1. Log in to one provider's dashboard (e.g., Polygon) and revoke the active API key.
2. Wait for the next scheduled collection cycle for that provider (up to 15 min for `polygon.equity`).
3. Confirm in `collector.log` that the runner logged the failure for that provider.
4. Confirm a `failed` row was written to the `collection_runs` table:
   ```powershell
   # From a Python REPL or SQLite shell in the project root:
   python -c "
   from alphamind.persistence.db import get_engine
   import sqlalchemy as sa
   engine = get_engine()
   with engine.connect() as conn:
       rows = conn.execute(sa.text(
           \"SELECT vendor, status, error_summary FROM collection_runs \"
           \"WHERE status = 'failed' ORDER BY started_at DESC LIMIT 5\"
       )).fetchall()
   print(rows)
   "
   ```
5. Confirm that collectors for other providers continued running (no full-process crash).
6. Re-issue a new key, update `.env`, and run `nssm restart alphamind-collector`.
7. Confirm the next cycle for the revoked provider reports `success` in `collection_runs`.

---

## Uninstall the service

Run from an elevated PowerShell session:

```powershell
.\scripts\uninstall_collector_service.ps1
```

Log files and configuration are preserved. To reinstall cleanly, run `install_collector_service.ps1` again.

---

## Troubleshooting quick reference

| Symptom | Where to look | Likely cause |
|---------|--------------|--------------|
| Service fails to start | `collector.err.log`, Windows Event Log | Missing `.env`, missing venv, Python import error |
| Collector logs HTTP 401 | `collector.log` | Expired or missing API key in `.env` |
| Collector logs HTTP 429 | `collector.log` | Rate limit exceeded — check `data_sources.yaml` throttle settings |
| Service restarts every 60 s | `collector.log`, `collector.err.log` | Uncaught exception at startup — read the traceback |
| `Get-Service` shows `StartPending` | Wait 30 s; if stuck, check `collector.err.log` | Slow Python startup or missing dependency |
| `%USERPROFILE%` resolves to SYSTEM | Re-run `nssm set ObjectName` step | Service account not configured after install |
