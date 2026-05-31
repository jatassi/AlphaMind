# collector/ — standalone data-collection service

Data layer. A long-running service (separate from the pipeline and monitor) that polls
`data_sources/` clients on a cadence and persists raw market/qualitative data for the
distillation layer to consume. Runs as an NSSM Windows service in production. Design
intent (historical): `docs/design/01-data-layer/collector/`.

## Key invariants

- Single responsibility: collect and store raw data. No distillation, no decisions.
- Failures must surface in the logs, not be swallowed — operators comb `collector.log` for silent failures.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
