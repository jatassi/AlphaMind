---
status: done
completed_date: 2026-04-27
commit_id: 7db16f2
---

# 07 — Resolved-config snapshot persistence

## Goal

Persist the resolved-config snapshot per `state-persistence.md § Composition state fields` and `configuration-management.md § Composition model`. Each invocation's resolver output is serialized to JSON, hashed (SHA-256), and written to a per-invocation filesystem location. The hash and reference path are returned for the caller to record on the invocation record (the database-side wiring is owned by the not-yet-built state-persistence implementation).

## Reading

- `docs/design/configuration-management.md` § Composition model — snapshot contract: persisted with SHA-256 hash and path recorded in the invocation record's provenance fields
- `docs/design/05-execution-layer/state-persistence.md` § Composition state fields — the receiving fields on the invocation record (`resolved_config_hash`, `resolved_config_snapshot_reference`, `feature_flags_snapshot`)
- `docs/design/05-execution-layer/state-persistence.md` § Filesystem snapshot layout — `data/provenance/invocations/{invocation_id}/resolved_config.json` (the canonical layout)
- `docs/architecture/infrastructure.md` § Layer 2 — `%USERPROFILE%\AlphaMind\` paths
- `src/alphamind/config/resolver.py` (post-story-05) — `ResolvedConfig` is the input

## Depends on

- 05 (resolver — `ResolvedConfig` is what gets persisted)

## Scope

In scope:
- `src/alphamind/config/snapshot.py` defining:
  - `SnapshotResult` (frozen dataclass): `hash: str`, `path: Path`, `feature_flags_snapshot: dict[str, bool]`
  - `serialize_resolved_config(resolved: ResolvedConfig) -> str` — produces a deterministic JSON string. Sort keys; serialize tuples as JSON arrays; serialize Pydantic models via their `model_dump_json()` method then re-parse + re-serialize through `json.dumps` with sorted keys to enforce determinism. The string is the canonical form hashed and persisted.
  - `compute_snapshot_hash(serialized: str) -> str` — `hashlib.sha256(serialized.encode("utf-8")).hexdigest()`. Returns the 64-hex-char digest.
  - `persist_snapshot(resolved: ResolvedConfig, *, archive_root: Path, invocation_id: str) -> SnapshotResult` — runs serialize → hash → write. Writes the JSON to `archive_root / "invocations" / invocation_id / "resolved_config.json"`. Creates intermediate directories. Returns the result.
  - `feature_flags_snapshot(resolved: ResolvedConfig) -> dict[str, bool]` — extracts the flat feature-flag map from `ResolvedConfig.feature_flags` for the invocation record's inline JSON column per `state-persistence.md § Composition state fields`.
- Determinism guarantees:
  - `serialize_resolved_config` is byte-identical for byte-identical inputs across invocations and across machines.
  - Floats are emitted at their canonical Python `repr()` precision; do not coerce to `str(round(x, n))`.
  - Tuples of agents and overlays are emitted in their order from `ResolvedConfig` (which preserves operator-supplied order). Determinism on this dimension is the resolver's job; the serializer copies the order.
  - StrEnum members serialize as their string value, not their member name (`AnalystOutputMode.watchlist` → `"watchlist"`).
- Path-handling care:
  - `archive_root` is the resolved value of `MainConfig.paths.archive` (the caller is responsible for resolving Windows `%USERPROFILE%` expansions before passing the path in).
  - Use `pathlib.Path.mkdir(parents=True, exist_ok=True)` — never `os.makedirs` — for cross-platform consistency.
  - Write atomically: write to `resolved_config.json.tmp`, fsync, then `Path.replace` to the final name. This avoids leaving a half-written file if the process crashes mid-write.
- Unit tests covering: a fixed `ResolvedConfig` produces a stable hash across two calls; serialization is round-trippable through `json.loads(json.dumps(...))`; `persist_snapshot` writes to the documented path layout; the atomic write avoids leaving `.tmp` files on success; `feature_flags_snapshot` returns a flat `dict[str, bool]` equal to the underlying `FeatureFlags` model's dump.

Out of scope:
- The invocation record itself — owned by the not-yet-built state-persistence implementation. This story produces the hash and path; the caller writes them to the database.
- Loading a snapshot for diff display — the command center owns that surface (deferred).
- Compression or compaction — JSON files are small (~10-20KB resolved); no compression needed.
- Garbage collection — the archive grows unboundedly across invocations; pruning is operator-managed and not in scope.

## Notes

**Why a deterministic JSON serializer.** The hash must be reproducible across runs of the same code on the same input. Python's default `json.dumps` is deterministic when `sort_keys=True` is passed and floats are not coerced. `Pydantic.model_dump_json()` honors model definition order, which is stable across runs but not lexicographic — re-parse the dump and re-emit through `json.dumps(sort_keys=True)` for canonical-form bytes.

**StrEnum serialization.** Pydantic v2 emits StrEnum members as their string value automatically when using `model_dump`. The dataclass-based `ResolvedConfig` does not — Python's default `dataclasses.asdict` returns the raw enum member, which `json.dumps` cannot serialize. The serializer must walk the dataclass fields and convert StrEnum members to their `.value` before passing to `json.dumps`. Use `dataclasses.asdict` with a `dict_factory` callback that handles the enum case.

**Path layout.** Per `state-persistence.md § Filesystem snapshot layout`, the canonical layout is `data/provenance/invocations/{invocation_id}/resolved_config.json`. `archive_root` parameter combines with `"invocations"` to produce the full path: callers pass `archive_root = paths.archive / "provenance"`. Reusing the existing `archive` path under `main.yaml` keeps the operator-facing layout consistent with `infrastructure.md § Layer 2`.

**Atomic write rationale.** A snapshot half-written when a process crashes (power loss, OS kill) would corrupt the invocation record's reference. The .tmp + replace pattern is OS-level atomic on macOS, Linux, and Windows for paths on the same filesystem.

## Acceptance criteria

- [ ] `src/alphamind/config/snapshot.py` exists and defines `SnapshotResult`, `serialize_resolved_config`, `compute_snapshot_hash`, `persist_snapshot`, `feature_flags_snapshot`.
- [ ] `models/__init__.py` (or `config/__init__.py`) re-exports the public names (`SnapshotResult`, `persist_snapshot`).
- [ ] A unit test asserts `serialize_resolved_config` returns the same string for the same `ResolvedConfig` twice in a row (byte-identical).
- [ ] A unit test asserts `compute_snapshot_hash` returns a 64-hex-character string.
- [ ] A unit test asserts the hash for a known fixture `ResolvedConfig` matches a pinned expected value (regression-fixture pattern — pin the hash so a serialization change forces a deliberate update).
- [ ] A unit test asserts `persist_snapshot` writes the JSON file to `archive_root / "invocations" / "{invocation_id}" / "resolved_config.json"`.
- [ ] A unit test asserts the persisted JSON file content equals `serialize_resolved_config(resolved)`.
- [ ] A unit test asserts `persist_snapshot` creates intermediate directories if absent.
- [ ] A unit test asserts no `.tmp` file remains after a successful `persist_snapshot` call.
- [ ] A unit test asserts StrEnum fields in `ResolvedConfig` serialize as their string value, not their member name.
- [ ] A unit test asserts `feature_flags_snapshot` returns a flat `dict[str, bool]` exactly equal to `FeatureFlags.model_dump()` keyed identically.
- [ ] A unit test asserts the JSON file is round-trippable: `json.loads(persisted_content)` produces an equal-shaped dict.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
