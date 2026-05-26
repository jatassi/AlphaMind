"""Append-only JSONL progress emitter for ``--debug-e2e`` mode.

Story 02c / ALP-499. Implements the ``ProgressEmitter`` Protocol
(introduced by story 01b / ALP-495) with a synchronous append-only JSONL
sink: one JSON line per event, ``flush() + os.fsync()`` after every
write so an in-flight debug invocation can be tailed externally.

Per parent issue ALP-493 § Pre-resolved (J) — observability beats
throughput here; buffering or async writes are out of scope.

Per parent issue ALP-493 § Pre-resolved (B) — the ``agent_response``
field set is fixed at the Protocol level (``duration_s``,
``input_tokens``, ``cache_read_tokens``, ``cache_write_tokens``,
``output_tokens``, ``tool_calls``, ``stop_reason``); this emitter
forwards every caller-supplied kwarg as-is. The three input-side
counts mirror the SDK ``usage`` split so an operator can confirm at a
glance that the prompt was assembled and cached (ALP-701).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["JsonlProgressEmitter"]


class JsonlProgressEmitter:
    """Append-only JSONL emitter satisfying ``ProgressEmitter``.

    One JSON line per event; ``flush() + os.fsync()`` after each write
    so an in-flight debug invocation can be tailed externally (per
    parent issue ALP-493 § Pre-resolved (J) — observability beats
    throughput).

    The parent directory is created on first write; the file is opened
    in append mode per emit so subsequent writes accumulate.

    Non-JSON-serializable field values raise ``TypeError`` at write
    time — there is no silent drop.
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
        # ``newline=""`` disables Windows ``\n`` -> ``\r\n`` text-mode
        # translation so the on-disk bytes are LF-only on every
        # platform; the downstream consumers (``json.loads``) tolerate
        # CRLF, but byte-identical output matters for archive digests
        # and any future binary-aware parser.
        with self._path.open("a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
