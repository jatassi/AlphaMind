"""Cross-package atomic-write primitives.

Hoisted from three duplicate ``_atomic_write`` implementations originally
living in ``config/snapshot.py``, ``distillation/calibration_snapshot.py``,
and ``config/control_handlers/profile_switch.py`` (ALP-473). The union of
their semantics:

1. Write to a sibling ``.tmp`` file (same directory, same filesystem).
2. ``flush`` + ``fsync`` the file descriptor so the bytes are durable.
3. ``Path.replace`` the tmp file onto the final path — OS-atomic on macOS,
   Linux, and Windows for in-filesystem renames.
4. ``fsync`` the parent directory on non-Windows hosts so the rename itself
   survives a power-loss event. Windows' POSIX shim rejects directory
   ``os.open`` with ``EACCES``; ``Path.replace`` is the strongest in-tree
   durability primitive Windows can offer.

The helpers also call ``parent.mkdir(parents=True, exist_ok=True)`` so the
target's directory tree is created on first write — the calibration-snapshot
caller needed this and the snapshot caller pre-created the directory, so the
kernel form unifies on creating it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["atomic_write_bytes", "atomic_write_text"]


def atomic_write_text(path: Path, contents: str) -> None:
    """Atomically write ``contents`` (UTF-8 encoded) to ``path``.

    See module docstring for the full semantics. Equivalent to::

        atomic_write_bytes(path, contents.encode("utf-8"))
    """
    atomic_write_bytes(path, contents.encode("utf-8"))


def atomic_write_bytes(path: Path, contents: bytes) -> None:
    """Atomically write raw ``contents`` to ``path``.

    See module docstring for the full semantics.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("wb") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)

    # Parent-directory fsync is a Linux-only durability primitive; Windows'
    # POSIX shim rejects ``os.open`` against directory paths with EACCES, and
    # ``Path.replace`` already provides the in-tree atomicity Windows can
    # offer.
    if sys.platform != "win32":
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
