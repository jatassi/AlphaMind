"""Context manager that writes a system prompt to a tempfile.

Used by the analyst, strategist, and PM harnesses to pass the system prompt
to the Claude Agent SDK in file-mode rather than as a CLI argument. The SDK
serializes ``ClaudeAgentOptions.system_prompt`` (when supplied as a string)
straight into the subprocess argv as ``--system-prompt <text>``. The three
decision-layer agents also set ``output_format`` to a ``json_schema`` mode
that adds a ``--json-schema <json>`` argument carrying a 15-16 KB JSON Schema
blob; combined with the 20 KB analyst / strategist / PM system prompts the
total cmdline crosses Windows ``CreateProcessW``'s 32,767-character limit and
``Popen`` aborts with a misleading ``FileNotFoundError: Claude Code not
found at: <path>`` error.

The SDK already supports a file-mode dict
(``{"type": "file", "path": <path>}``) for ``system_prompt`` — see
``claude_agent_sdk._internal.transport.subprocess_cli._build_command``. Passing
the prompt via this dict moves the 20 KB blob OFF the cmdline; only the file
path goes through argv, dropping the combined size to ~16 KB which is well
under the limit. macOS handles longer cmdlines (``ARG_MAX`` is in the
megabytes), so the file mode is functionally identical there — the rewrite is
unconditional rather than platform-gated for behavioural parity across dev
and prod.

The synthesizer and the four analysis-layer researchers do not pass
``output_format``, so their cmdline never approaches the limit; they
intentionally continue to pass ``system_prompt`` as a string.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["system_prompt_as_file"]


@contextmanager
def system_prompt_as_file(prompt_text: str) -> Iterator[str]:
    """Write *prompt_text* to a tempfile and yield its path, cleaning up on exit.

    Uses ``tempfile.mkstemp`` (rather than ``NamedTemporaryFile``) because the
    SDK subprocess must open the file independently of this process —
    ``NamedTemporaryFile``'s default ``delete=True`` keeps the handle open and
    on Windows that blocks any other process from opening the same path. The
    ``finally`` block unlinks the path; ``missing_ok=True`` makes the cleanup
    tolerant of the rare case where the OS has already reaped the file (e.g.,
    on shutdown paths).
    """
    fd, path = tempfile.mkstemp(suffix=".md", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(prompt_text)
        yield path
    finally:
        Path(path).unlink(missing_ok=True)
