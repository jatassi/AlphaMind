"""Shared helpers for unwrapping :class:`BaseExceptionGroup` (ALP-469).

The TaskGroup migration introduced several call sites that catch the
``BaseExceptionGroup`` raised by ``asyncio.TaskGroup.__aexit__`` and need
to surface the first non-``CancelledError`` leaf — ``CancelledError``
children are an artifact of the cancellation cascade, not the originating
failure. Centralising the helper keeps the policy consistent across the
scheduler supervisor, continuous-monitor supervisor, and underlying-stream
task host.
"""

from __future__ import annotations

import asyncio


def first_non_cancelled(eg: BaseExceptionGroup) -> BaseException | None:
    """Return the first leaf exception in *eg* that isn't ``CancelledError``.

    Recurses through nested :class:`BaseExceptionGroup` children.
    ``asyncio.TaskGroup`` re-raises ``CancelledError`` siblings alongside
    the real failure when a crash triggers sibling cancellation; surface
    the real failure so callers see the same exception they did before
    the structured-concurrency migration.
    """
    for exc in eg.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            nested = first_non_cancelled(exc)
            if nested is not None:
                return nested
        elif not isinstance(exc, asyncio.CancelledError):
            return exc
    return None
