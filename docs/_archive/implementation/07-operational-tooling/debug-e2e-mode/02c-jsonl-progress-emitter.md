# 02c — JSONL progress emitter

## Goal

Implement `scheduler/debug_e2e/jsonl_emitter.py` with `JsonlProgressEmitter(*, path)` — a four-method class satisfying the `ProgressEmitter` Protocol from story 01b. Each method opens the target file in append mode, writes one JSON line per event with a wall-clock timestamp, then `flush()` + `os.fsync()` per parent Issue Pre-resolved decision § (J). The parent directory is created on first write.

## Reading

* `docs/design/debug-e2e-mode.md` § 3 (Package layout — `jsonl_emitter.py` location), § 5 (Testing seam — JSONL output validated by integration test reading the file back)
* `ALP-495` (01b) — provides `ProgressEmitter` Protocol this story satisfies
* Parent issue `ALP-493` Pre-resolved decisions §§ (B), (J) — `agent_response` field set, synchronous-write contract

## Depends on

* `ALP-495` (01b — Progress Protocol + RunInvocationContext debug_e2e field)

## Scope

In scope: `src/alphamind/scheduler/debug_e2e/jsonl_emitter.py` (new). Tests at `tests/scheduler/debug_e2e/test_jsonl_emitter.py`.

### 1\. `scheduler/debug_e2e/jsonl_emitter.py`

```python
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonlProgressEmitter:
    """Append-only JSONL emitter satisfying ProgressEmitter.

    One JSON line per event; ``flush() + os.fsync()`` after each write so an
    in-flight debug invocation can be tailed externally (per parent Issue
    Pre-resolved decision § (J) — observability beats throughput).
    """

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def phase_start(self, phase: str) -> None:
        self._write({"event": "phase_start", "phase": phase})

    def phase_done(self, phase: str, **fields: Any) -> None:
        self._write({"event": "phase_done", "phase": phase, **fields})

    def agent_request(self, **fields: Any) -> None:
        self._write({"event": "agent_request", **fields})

    def agent_response(self, **fields: Any) -> None:
        self._write({"event": "agent_response", **fields})

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload["timestamp"] = datetime.now(UTC).isoformat()
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
```

Every event has a top-level `event` discriminator (`phase_start` / `phase_done` / `agent_request` / `agent_response`), a wall-clock `timestamp` field (ISO-8601 UTC), and the method's caller-supplied fields. Non-JSON-serializable values raise `TypeError` at write time — no silent drop.

### Out of scope

* The `configure_debug_e2e` factory that constructs `JsonlProgressEmitter` per invocation (story 03).
* Truncation / rotation of the JSONL file (out of scope — operator deletes the archive directory between debug runs).
* Async / buffered writes (forbidden per parent issue § (J)).

## Acceptance criteria

- [ ] `src/alphamind/scheduler/debug_e2e/jsonl_emitter.py` exposes `JsonlProgressEmitter(*, path: Path)`.
- [ ] `JsonlProgressEmitter` satisfies `ProgressEmitter` — verified by `isinstance` check in tests (use `@runtime_checkable` on the Protocol per story 01b's shape).
- [ ] Each emitted event is one JSON line containing `event`, `timestamp` (ISO-8601 UTC), plus the method's caller-supplied fields.
- [ ] Parent directory is created on first write; subsequent writes append.
- [ ] Each write is followed by `f.flush() + os.fsync()` — verified by a test that opens the file from a second handle after each emit, reads the line back, and asserts the event content before the context manager exits the writer.
- [ ] `tests/scheduler/debug_e2e/test_jsonl_emitter.py` emits one event of each type, re-reads the file, asserts the parsed JSONL matches the emitted sequence in order.
- [ ] `uv run pytest tests/scheduler/debug_e2e/test_jsonl_emitter.py -n auto` passes; full linter chain clean.

## Verification

`uv run pytest tests/scheduler/debug_e2e/test_jsonl_emitter.py -n auto` passes. Linter chain clean.