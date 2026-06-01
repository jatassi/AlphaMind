# 09 — VPS Caddy + WireGuard remote access (placeholder, deferred)

## Status

**Placeholder.** Deferred from [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) v1 per the parent's § Pre-resolved (B). Do not dispatch — re-draft into a proper work tree (or a single story) once the operator is ready to do the remote-access handover.

## What this work tree owns

The public-internet path from the operator's browser to the command center per [command-center.md § Authentication and access — Remote access](<docs/design/command-center.md>):

```
browser → Cloudflare DNS (grey cloud) → VPS:443 → Caddy (TLS + headers + rate limit)
       → WireGuard tunnel (wg0, 10.8.0.0/24) → command center on trading machine
```

Operator-maintained VPS-side artifacts (lives outside this repo):

* **WireGuard peer entry** added to `/etc/wireguard/wg0.conf` on the VPS for the trading machine.
* **Caddy site block** for the chosen subdomain of `atassi.org` (e.g., `commandcenter.atassi.org`) per the snippet in command-center.md § Remote access — `import security_headers`, `rate_limit /auth* { 10r/m }` and `rate_limit /* { 300r/m }`, `reverse_proxy 10.8.0.<peer>:<port>`.
* **Cloudflare DNS** record for the subdomain pointing at the VPS (DNS-only, grey cloud).
* `wg0` **peer** on the trading machine joined to the VPS hub.

In-repo deliverables:

* Switch `command-center.yaml`'s `bind.host` from `127.0.0.1` to the trading machine's WG IP (the WG-IP value is operator-supplied at handover; no hardcoded value).
* Extend `scripts/RUNBOOK_command_center.md` with the "Remote access bring-up" section: WG peer install on the trading machine, WG peer entry on the VPS, Caddy block install, Cloudflare DNS record, end-to-end verification (browser hits the subdomain → command center loads → passkey login works).
* Optional: a `scripts/verify_remote_access.py` that hits the public URL from outside the trading machine and asserts a 200 on the login page (gated on the operator providing the subdomain + a test passkey).

## Why deferred

[ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) v1 ships local-only (loopback + LAN bind) per the operator's Q2 answer at drafting time. The VPS-side artifacts are operator-maintained outside this repo and have their own bring-up cadence (the operator chooses when to do the WG + Caddy work); deferring keeps the v1 scope tight and lets the operator pick the timing.

## Depends on

* [ALP-685](<https://linear.app/alphamind-jatassi/issue/ALP-685>) (07 verify + RUNBOOK + NSSM, this work tree) — command center v1 must be stood up local-only first; remote access extends it.

## Action when unblocked

Likely a single story (no decomposition needed): runbook section + bind-host config field + smoke verify. If the operator wants the in-repo `verify_remote_access.py` script, that's a second story or in-scope-of-the-first. Re-draft via `/draft-user-stories Command center — remote access follow-on` or land as a one-off story directly under [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center).

## Acceptance criteria

- [ ] Do not implement against this placeholder. Re-draft or write a single proper story first.

## Verification

n/a — placeholder.