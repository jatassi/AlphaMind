"""
Cross-cutting primitives for the AlphaMind data-sources library.

Exposes:
- ``load_config`` — one-shot config loader
- ``RetryShape`` — enum matching the three tiers from api-failure-handling.md
- ``with_retries`` — decorator factory implementing per-tier retry behaviour
- ``RateLimiter`` — thread-safe token-bucket per provider
- ``track_run`` — context manager that writes to ``collection_runs``
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Generator, TypeVar

import httpx
from dotenv import dotenv_values

from alphamind.config.models import (
    CollectorScheduleConfig,
    DataSourcesConfig,
    NewsOutletsConfig,
)

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlphaMindConfig:
    """Immutable aggregate of the three YAML config sections."""

    data_sources: DataSourcesConfig
    collector_schedule: CollectorScheduleConfig
    news_outlets: NewsOutletsConfig


def load_config(
    config_dir: str | None = None,
    env_file: str | None = None,
) -> AlphaMindConfig:
    """
    Load and validate all three YAML config files plus the ``.env`` file.

    Parameters
    ----------
    config_dir:
        Directory containing ``data_sources.yaml``, ``collector_schedule.yaml``,
        and ``news_outlets.yaml``.  Defaults to ``<repo-root>/config/``.
    env_file:
        Path to the ``.env`` file.  Defaults to ``<repo-root>/.env``.

    Raises
    ------
    EnvironmentError
        If a provider's ``api_key_env`` reference names an environment variable
        that is not present in the resolved ``.env``.
    """
    import yaml  # type: ignore[import-untyped]

    root = Path(__file__).parents[3]
    cfg_dir = Path(config_dir) if config_dir is not None else root / "config"
    dot_env = Path(env_file) if env_file is not None else root / ".env"

    # Load env vars from .env (does not affect os.environ — keeps it pure)
    env_values: dict[str, str | None] = dotenv_values(str(dot_env))

    def _read(name: str) -> dict[str, Any]:
        path = cfg_dir / name
        with path.open() as fh:
            return yaml.safe_load(fh) or {}

    data_sources = DataSourcesConfig.model_validate(_read("data_sources.yaml"))
    collector_schedule = CollectorScheduleConfig.model_validate(
        _read("collector_schedule.yaml")
    )
    news_outlets = NewsOutletsConfig.model_validate(_read("news_outlets.yaml"))

    # Validate that every api_key_env reference resolves
    for name, provider in data_sources.providers.items():
        if provider.api_key_env is not None:
            if provider.api_key_env not in env_values or env_values[provider.api_key_env] is None:
                raise EnvironmentError(
                    f"Provider {name!r} requires environment variable "
                    f"{provider.api_key_env!r} but it is not set in {dot_env}"
                )

    return AlphaMindConfig(
        data_sources=data_sources,
        collector_schedule=collector_schedule,
        news_outlets=news_outlets,
    )


# ---------------------------------------------------------------------------
# RetryShape
# ---------------------------------------------------------------------------


class RetryShape(str, Enum):
    """Retry tier matching api-failure-handling.md § Criticality tiers."""

    critical = "critical"
    important = "important"
    optional = "optional"


# ---------------------------------------------------------------------------
# Retryable / non-retryable classification
# ---------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    """Return True when ``exc`` is a transient error worth retrying."""
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 429 and all 5xx are retryable; auth/permission 4xx are not
        return status == 429 or status >= 500
    return False


# ---------------------------------------------------------------------------
# with_retries
# ---------------------------------------------------------------------------

# Default attempt counts per shape (matches data_sources.yaml retry_shapes)
_SHAPE_ATTEMPTS: dict[RetryShape, int] = {
    RetryShape.critical: 3,
    RetryShape.important: 2,
    RetryShape.optional: 2,  # "single retry" == 1 retry → 2 total attempts
}

_SHAPE_INITIAL_DELAY: dict[RetryShape, float] = {
    RetryShape.critical: 1.0,
    RetryShape.important: 1.0,
    RetryShape.optional: 0.5,
}

_SHAPE_BACKOFF_MULTIPLIER: dict[RetryShape, float] = {
    RetryShape.critical: 2.0,
    RetryShape.important: 2.0,
    RetryShape.optional: 1.0,  # no backoff growth for optional
}


def with_retries(
    shape: RetryShape,
    *,
    _sleep: Callable[[float], None] = time.sleep,
) -> Callable[[F], F]:
    """
    Decorator factory that wraps a callable with per-tier retry behaviour.

    Parameters
    ----------
    shape:
        Retry tier — ``critical``, ``important``, or ``optional``.
    _sleep:
        Injectable sleep function (use ``lambda s: None`` in tests).
    """
    max_attempts = _SHAPE_ATTEMPTS[shape]
    initial_delay = _SHAPE_INITIAL_DELAY[shape]
    multiplier = _SHAPE_BACKOFF_MULTIPLIER[shape]

    def decorator(fn: F) -> F:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            last_exc: BaseException | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:
                    if not _is_retryable(exc):
                        raise
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        _sleep(delay)
                        delay *= multiplier
            assert last_exc is not None
            raise last_exc

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# RateLimiter — continuous token-bucket per provider
# ---------------------------------------------------------------------------


@dataclass
class _BucketState:
    rate_per_second: float
    capacity: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.last_refill = time.monotonic()


class RateLimiter:
    """
    Per-provider continuous token-bucket rate limiter.

    Thread-safe — ``acquire()`` may be called from multiple threads
    simultaneously.  Each ``acquire()`` consumes one token.  When the
    bucket is empty, ``acquire()`` blocks until enough time has elapsed
    for one token to become available.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _BucketState] = {}
        self._registry_lock = threading.Lock()

    def set_limit(self, provider: str, *, rate_per_minute: int) -> None:
        """Register or update the rate limit for *provider*."""
        rate_per_second = rate_per_minute / 60.0
        capacity = float(rate_per_minute)
        with self._registry_lock:
            self._buckets[provider] = _BucketState(
                rate_per_second=rate_per_second,
                capacity=capacity,
            )

    def acquire(self, provider: str) -> None:
        """
        Block until one token is available for *provider*, then consume it.

        If *provider* has not been registered via :meth:`set_limit`, the call
        returns immediately (unlimited).
        """
        with self._registry_lock:
            bucket = self._buckets.get(provider)
        if bucket is None:
            return

        while True:
            with bucket.lock:
                now = time.monotonic()
                elapsed = now - bucket.last_refill
                # Refill proportionally to elapsed time
                bucket.tokens = min(
                    bucket.capacity,
                    bucket.tokens + elapsed * bucket.rate_per_second,
                )
                bucket.last_refill = now

                if bucket.tokens >= 1.0:
                    bucket.tokens -= 1.0
                    return

                # Calculate how long to wait for one token
                deficit = 1.0 - bucket.tokens
                wait = deficit / bucket.rate_per_second

            time.sleep(wait)


# ---------------------------------------------------------------------------
# track_run
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    """Mutable state object yielded by :func:`track_run`."""

    rows_written: int = 0


class _DefaultRepo:
    """
    Production repository — writes to the SQLAlchemy ``collection_runs`` table.

    Imported lazily so that importing ``_common`` does not force a DB
    connection at module load time.
    """

    def __init__(self) -> None:
        from alphamind.persistence.models import Base, CollectionRuns
        from alphamind.persistence.session import make_engine, make_session_factory

        engine = make_engine()
        Base.metadata.create_all(engine)
        self._Session = make_session_factory(engine)
        self._model = CollectionRuns

    def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
        with self._Session() as sess:
            sess.add(
                self._model(
                    run_id=run_id,
                    collector=collector,
                    started_at=started_at,
                    status="running",
                )
            )
            sess.commit()

    def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
        with self._Session() as sess:
            row = sess.get(self._model, run_id)
            if row is not None:
                row.status = "success"
                row.completed_at = completed_at
                row.rows_written = rows_written
                sess.commit()

    def update_failed(self, run_id: str, error_summary: str) -> None:
        with self._Session() as sess:
            row = sess.get(self._model, run_id)
            if row is not None:
                row.status = "failed"
                row.error_summary = error_summary
                sess.commit()


@contextmanager
def track_run(
    collector_name: str,
    *,
    _repo: Any = None,
) -> Generator[RunState, None, None]:
    """
    Context manager that tracks a collection run in ``collection_runs``.

    Parameters
    ----------
    collector_name:
        Human-readable name for the collector (e.g. ``"polygon.equity"``).
    _repo:
        Optional repository override for testing.  When *None*, a
        :class:`_DefaultRepo` connected to the configured SQLite database
        is used.

    Yields
    ------
    RunState
        Mutable object — callers set ``run.rows_written`` before exiting.
    """
    repo = _repo if _repo is not None else _DefaultRepo()
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()

    repo.insert_running(run_id, collector_name, started_at)

    run = RunState()
    try:
        yield run
    except BaseException as exc:
        error_summary = f"{type(exc).__name__}: {exc}"
        repo.update_failed(run_id, error_summary)
        raise
    else:
        completed_at = datetime.now(UTC).isoformat()
        repo.update_success(run_id, completed_at, run.rows_written)
