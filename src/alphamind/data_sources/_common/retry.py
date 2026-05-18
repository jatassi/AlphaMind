"""Retry-tier vocabulary and the ``with_retries`` decorator factory.

The retry tier matches api-failure-handling.md § Criticality tiers. The
``vendor_outage_extended`` member is a fourth tier outside the standard
matrix — use it for idempotent GET-style fetches against vendors prone to
multi-minute 5xx outages (e.g. FRED) where the standard ``critical``
1s/2s schedule exhausts before the vendor recovers.
"""

from __future__ import annotations

import time
import urllib.error
from collections.abc import Callable
from enum import StrEnum
from typing import ParamSpec, TypeVar

import httpx
import requests.exceptions

P = ParamSpec("P")
R = TypeVar("R")

__all__ = ["RetryShape", "with_retries"]


class RetryShape(StrEnum):
    """Retry tier matching api-failure-handling.md § Criticality tiers.

    ``vendor_outage_extended`` is a fourth tier outside the criticality matrix:
    use it for idempotent GET-style fetches against vendors prone to multi-
    minute 5xx outages (e.g. FRED) where the standard ``critical`` 1s/2s
    schedule exhausts before the vendor recovers. Costs ~65 s of in-line wait
    in the worst case, so reserve it for collectors whose cadence is hours,
    not seconds.
    """

    critical = "critical"
    important = "important"
    optional = "optional"
    vendor_outage_extended = "vendor_outage_extended"


# ---------------------------------------------------------------------------
# Retryable / non-retryable classification
# ---------------------------------------------------------------------------


_FREDAPI_RETRYABLE_MESSAGES: tuple[str, ...] = (
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "too many requests",
)


def _is_transient_status(code: int) -> bool:
    """429 and all 5xx HTTP statuses are retryable; auth/permission 4xx are not."""
    return code == 429 or code >= 500


# Transport-layer transients that surface without an HTTP status, across the
# three HTTP clients in use: httpx (most collectors), urllib (fredapi), and
# requests (finnhub-python). All share the vendor-outage blast radius and the
# same idempotent-GET retry shape, so they collapse to one branch.
#
# - ``httpx.TimeoutException`` covers Connect/Read/Write/Pool timeouts.
# - ``httpx.NetworkError`` covers ConnectError (DNS / connection-refused),
#   ReadError, WriteError, and CloseError.
# - ``urllib.error.URLError`` / bare ``ConnectionResetError`` is the
#   urllib-side equivalent.
# - ``requests.exceptions.ConnectionError`` and ``requests.exceptions.Timeout``
#   cover the requests-backed SDKs (incl. ReadTimeout / ConnectTimeout
#   subclasses).
_TRANSIENT_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    urllib.error.URLError,
    ConnectionResetError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def _is_retryable(exc: BaseException) -> bool:
    """Return True when ``exc`` is a transient error worth retrying."""
    # HTTPError must be matched before URLError (its parent) so 4xx auth
    # failures don't fall through to the unconditional-transient branch.
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_transient_status(exc.response.status_code)
    if isinstance(exc, urllib.error.HTTPError):
        return _is_transient_status(exc.code)
    if isinstance(exc, _TRANSIENT_TRANSPORT_EXCEPTIONS):
        return True
    if isinstance(exc, ValueError):
        # fredapi wraps urllib.HTTPError as ValueError; original is on __context__.
        cause = exc.__context__
        return any(p in str(exc).lower() for p in _FREDAPI_RETRYABLE_MESSAGES) or (
            isinstance(cause, urllib.error.HTTPError) and _is_transient_status(cause.code)
        )
    return False


# ---------------------------------------------------------------------------
# with_retries
# ---------------------------------------------------------------------------

# Canonical retry schedule per tier — see api-failure-handling.md § Retry
# semantics and data-sources.md § Retry-per-tier.
_SHAPE_ATTEMPTS: dict[RetryShape, int] = {
    RetryShape.critical: 3,  # 2 retries → sleeps of 1.0s, 2.0s
    RetryShape.important: 2,  # 1 retry  → sleep  of 1.0s
    RetryShape.optional: 2,  # 1 retry  → sleep  of 0.5s
    RetryShape.vendor_outage_extended: 4,  # 3 retries → sleeps of 5s, 15s, 45s
}

_SHAPE_INITIAL_DELAY: dict[RetryShape, float] = {
    RetryShape.critical: 1.0,
    RetryShape.important: 1.0,
    RetryShape.optional: 0.5,
    RetryShape.vendor_outage_extended: 5.0,
}

_SHAPE_BACKOFF_MULTIPLIER: dict[RetryShape, float] = {
    RetryShape.critical: 2.0,
    RetryShape.important: 2.0,
    RetryShape.optional: 1.0,  # flat — Optional uses brief, non-growing delay
    RetryShape.vendor_outage_extended: 3.0,
}


def with_retries(
    shape: RetryShape,
    *,
    _sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a callable with per-tier retry behaviour.

    Parameters
    ----------
    shape:
        Retry tier — ``critical``, ``important``, ``optional``, or
        ``vendor_outage_extended``.
    _sleep:
        Injectable sleep function (use ``lambda s: None`` in tests).
    """
    max_attempts = _SHAPE_ATTEMPTS[shape]
    initial_delay = _SHAPE_INITIAL_DELAY[shape]
    multiplier = _SHAPE_BACKOFF_MULTIPLIER[shape]

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            delay = initial_delay
            last_exc: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:
                    # Retry classifier per runtime §G1: ``BaseException`` (vs
                    # ``Exception``) is intentional — ``_is_retryable``
                    # explicitly returns ``False`` for ``CancelledError`` /
                    # ``KeyboardInterrupt`` so they propagate. The wide catch
                    # exists because vendor SDKs (``fredapi``, ``urllib``)
                    # raise across multiple top-level hierarchies and the
                    # classifier must inspect each before deciding.
                    if not _is_retryable(exc):
                        raise
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        _sleep(delay)
                        delay *= multiplier
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
