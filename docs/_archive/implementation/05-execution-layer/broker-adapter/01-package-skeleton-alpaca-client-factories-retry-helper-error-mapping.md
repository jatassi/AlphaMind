# 01 — Package skeleton + Alpaca client factories + retry helper + error mapping

## Goal

Stand up `src/alphamind/execution/broker_adapter/` and `src/alphamind/execution/venue_configuration/` packages with the foundational pieces every other story consumes: a paper/live `AlpacaClientFactory` resolving credentials from `VenueConfig` + environment variables, a `submit_with_retry(...)` helper consuming `ExecutionConfig.submission_retry_window_seconds`, a typed result discriminator (`Submitted` / `GatewaySubmissionFailed`), and an Alpaca-error → adapter-rejection mapping aligned with `broker-adapter.md § Order submission`. No actual order translation, no actual REST calls — this story ships only the substrate.

## Reading

* `docs/design/05-execution-layer/broker-adapter.md` § Environment selection — paper vs live URL switching contract.
* `docs/design/05-execution-layer/broker-adapter.md` § Order submission — rejection reasons (`422` / `403` codes) the adapter must map to typed outcomes.
* `docs/design/05-execution-layer/state-persistence.md` § Phase 2 write path — submission retry window contract: brief exponential-backoff loop bounded by configured window; on exhaustion, command transaction rolls back and a `command_abandoned` activity-log entry surfaces to the originating agent at the next invocation.
* `src/alphamind/config/models/venue.py` — `VenueConfig`, `Alpaca`, `AlpacaCredentials`. Already shipped; this story consumes them.
* `src/alphamind/config/models/execution.py` — `ExecutionConfig.submission_retry_window_seconds` (currently 30s). Already shipped; consumed by the retry helper.
* `config/venue.yaml`, `config/execution.yaml`, `config/main.yaml` — runtime values; `main.yaml`'s `execution_mode: paper | live` is the dispatcher.
* `.env.example` — confirms `ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET` / `ALPACA_LIVE_KEY` / `ALPACA_LIVE_SECRET` placeholders are present.
* `pyproject.toml` — confirms `alpaca-py>=0.32` is in deps (currently 0.43.4 in `uv.lock`). Inspect `alpaca.trading.client.TradingClient` and `alpaca.trading.stream.TradingStream` constructors for keyword shape.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` — read the engine-stub to understand the existing `Submitted` / rejection shape so this story's typed outcomes compose with it (story 03e is the actual swap; this story just lines up the type).
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (A), (C), (I) — substrate decisions this story applies.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates.

## Scope

Source under `src/alphamind/execution/broker_adapter/` (new files for client factory + retry helper + error mapping + typed outcomes). Tests at `tests/execution/broker_adapter/`.

### 1\. Package layout

Create the following module skeleton — each empty-ish module gets a docstring and the typed exports listed below; subsequent stories fill them in:

```
src/alphamind/execution/broker_adapter/
    __init__.py                   # re-exports the public surface
    client_factory.py             # AlpacaClientFactory + build_*_client
    retry.py                      # submit_with_retry + Submitted / GatewaySubmissionFailed
    errors.py                     # AlpacaError → typed adapter rejection mapping
    # — order_*.py modules added by 02b/c/d/e —
    # — fill_stream.py added by 02f —
    # — recovery.py added by 03d —
src/alphamind/execution/venue_configuration/
    __init__.py                   # placeholder re-exports; populated by 02g
    # — constants.py added by 02g —
    # — calendar_cache.py added by 03a —
    # — account_state.py added by 03b —
    # — settlement.py added by 04a —
```

### 2\. `AlpacaClientFactory`

Public type + factory. Ships in `client_factory.py`.

```python
import os
from dataclasses import dataclass
from typing import Literal

from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream

from alphamind.config.models.venue import AlpacaCredentials, VenueConfig


ExecutionMode = Literal["paper", "live"]


@dataclass(frozen=True)
class ResolvedCredentials:
    """Resolved API key/secret for the selected execution mode."""

    mode: ExecutionMode
    api_key: str
    api_secret: str
    rest_url: str
    ws_url: str


class AlpacaClientFactory:
    """Builds TradingClient / TradingStream instances for the selected execution mode.

    Resolves API credentials from environment variables named in
    VenueConfig.alpaca.<mode>; raises if the named env vars are unset or
    empty. Frozen — one factory instance per adapter lifetime.
    """

    def __init__(self, venue: VenueConfig, mode: ExecutionMode) -> None:
        creds_block: AlpacaCredentials = (
            venue.alpaca.paper if mode == "paper" else venue.alpaca.live
        )
        api_key = os.environ.get(creds_block.api_key_env, "")
        api_secret = os.environ.get(creds_block.api_secret_env, "")
        if not api_key or not api_secret:
            missing = [
                name
                for name, value in (
                    (creds_block.api_key_env, api_key),
                    (creds_block.api_secret_env, api_secret),
                )
                if not value
            ]
            msg = (
                f"Alpaca {mode} credentials not set: "
                f"environment variable(s) {missing} are unset or empty"
            )
            raise RuntimeError(msg)
        self._credentials = ResolvedCredentials(
            mode=mode,
            api_key=api_key,
            api_secret=api_secret,
            rest_url=creds_block.rest_url,
            ws_url=creds_block.ws_url,
        )

    @property
    def credentials(self) -> ResolvedCredentials:
        return self._credentials

    def build_trading_client(self) -> TradingClient:
        """Return a fresh TradingClient bound to the resolved credentials."""
        return TradingClient(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            paper=(self._credentials.mode == "paper"),
            url_override=self._credentials.rest_url,
        )

    def build_trading_stream(self) -> TradingStream:
        """Return a fresh TradingStream bound to the resolved credentials."""
        return TradingStream(
            api_key=self._credentials.api_key,
            secret_key=self._credentials.api_secret,
            paper=(self._credentials.mode == "paper"),
            url_override=self._credentials.ws_url,
        )
```

### 3\. `submit_with_retry` helper + typed outcomes

Ships in `retry.py`.

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Submitted(Generic[T]):
    """A submission attempt that produced an Alpaca acknowledgment."""

    payload: T
    attempt_count: int


@dataclass(frozen=True)
class GatewaySubmissionFailed:
    """A submission attempt that exhausted the retry window without success.

    The OMS Phase 2 write path translates this into a `command_abandoned`
    activity-log entry per state-persistence.md.
    """

    reason: str
    attempt_count: int
    last_error_class: str  # short identifier, e.g., "APITimeoutError"


SubmissionOutcome = Submitted[T] | GatewaySubmissionFailed


async def submit_with_retry(
    submit: Callable[[], Awaitable[T]],
    *,
    window_seconds: int,
    transient_classifier: Callable[[BaseException], bool] | None = None,
) -> SubmissionOutcome[T]:
    """Run ``submit()`` with exponential-backoff retry within the time window.

    * Total wall-clock budget: ``window_seconds`` (consumed from
      ``ExecutionConfig.submission_retry_window_seconds``).
    * Backoff: 0.5s, 1s, 2s, 4s, capped at half the remaining window.
    * Transient errors (network errors, 5xx) are retried; permanent errors
      (4xx with rejection reasons per ``broker-adapter.md``) are NOT — the
      caller maps those via ``errors.py`` and surfaces to the OMS as
      synchronous rejections.
    * ``transient_classifier`` defaults to a function in ``errors.py``.
    """
    ...
```

The helper's design has one subtle invariant: 4xx rejections (e.g., 422 validation, 403 insufficient_buying_power) are **synchronous OMS rejections** — they should not be retried. The helper distinguishes by inspecting the exception class. Bake the classifier default into `errors.py`.

### 4\. Alpaca error mapping

Ships in `errors.py`. Maps `alpaca.common.exceptions.APIError` and network-level exceptions into the adapter's typed taxonomy:

```python
from dataclasses import dataclass
from typing import Literal


PermanentRejectionCode = Literal[
    "validation_failed",            # 422
    "insufficient_buying_power",    # 403
    "insufficient_shares",          # 403 (short)
    "options_level_not_approved",   # 403 options
    "contract_expired",             # 422 options
    "underlying_halted",            # 403 options
    "invalid_legs",                 # 422 mleg
    "asset_not_tradable",           # 403 / 422 generic
    "other_permanent",              # fallback
]


@dataclass(frozen=True)
class PermanentRejection:
    """Alpaca rejected the submission for a non-retriable reason."""

    code: PermanentRejectionCode
    http_status: int
    alpaca_message: str


def classify_alpaca_error(exc: BaseException) -> PermanentRejection | None:
    """Return PermanentRejection if the exception is non-retriable; else None.

    None means the caller's retry loop should retry. Network errors,
    timeouts, and 5xx responses return None. APIError instances are
    inspected for their HTTP status and Alpaca's error-code field.
    """
    ...


def is_transient(exc: BaseException) -> bool:
    """The default transient classifier for ``submit_with_retry``."""
    return classify_alpaca_error(exc) is None
```

The exact `code` values map from the rejection-reasons section of broker-adapter.md. Each maps to a stable string the OMS surfaces to the PM in the synchronous rejection payload.

### 5\. Public surface re-exports

In `broker_adapter/__init__.py`:

```python
from alphamind.execution.broker_adapter.client_factory import (
    AlpacaClientFactory,
    ExecutionMode,
    ResolvedCredentials,
)
from alphamind.execution.broker_adapter.errors import (
    PermanentRejection,
    PermanentRejectionCode,
    classify_alpaca_error,
    is_transient,
)
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    Submitted,
    SubmissionOutcome,
    submit_with_retry,
)

__all__ = [
    "AlpacaClientFactory",
    "ExecutionMode",
    "GatewaySubmissionFailed",
    "PermanentRejection",
    "PermanentRejectionCode",
    "ResolvedCredentials",
    "Submitted",
    "SubmissionOutcome",
    "classify_alpaca_error",
    "is_transient",
    "submit_with_retry",
]
```

### 6\. RUNBOOK seed

Add `scripts/RUNBOOK_broker_adapter.md` with a stub: title, prerequisites placeholder (will be filled by story 05), a one-line "What to expect" section noting that this skeleton story does not yet support a runnable verify script. Story 05 replaces this file's content; this story just creates the shell so subsequent runbook references resolve.

### Out of scope

* Any actual REST endpoint wrapper (story 02a covers GETs, 02b/c/d/e cover POSTs/PATCH/DELETE).
* Any websocket subscription logic (story 02f).
* Any venue-config constants (story 02g).
* Phase 1 / Phase 2 write-path edits (stories 03c, 03e, 04b).
* Backwards-compatibility shims — there are no prior consumers of these types.

## Acceptance criteria

- [ ] `src/alphamind/execution/broker_adapter/__init__.py` exports `AlpacaClientFactory`, `ExecutionMode`, `ResolvedCredentials`, `Submitted`, `GatewaySubmissionFailed`, `SubmissionOutcome`, `submit_with_retry`, `PermanentRejection`, `PermanentRejectionCode`, `classify_alpaca_error`, `is_transient` and they are importable.
- [ ] `src/alphamind/execution/venue_configuration/__init__.py` exists with a docstring naming the package's role; populated symbol list deferred to story 02g.
- [ ] `AlpacaClientFactory(venue, mode="paper")` reads `ALPACA_PAPER_KEY` and `ALPACA_PAPER_SECRET` from `os.environ` and stores them in `ResolvedCredentials`.
- [ ] `AlpacaClientFactory(venue, mode="live")` reads `ALPACA_LIVE_KEY` / `ALPACA_LIVE_SECRET` from `os.environ`.
- [ ] Constructing the factory with the named env vars unset (or empty string) raises `RuntimeError` whose message names the missing variable(s).
- [ ] `factory.build_trading_client()` returns an `alpaca.trading.client.TradingClient` instance bound to the resolved credentials and the `paper=True/False` flag matching `mode`.
- [ ] `factory.build_trading_stream()` returns an `alpaca.trading.stream.TradingStream` instance bound to the resolved credentials.
- [ ] `Submitted` and `GatewaySubmissionFailed` are frozen dataclasses; `SubmissionOutcome[T]` is the union alias.
- [ ] `submit_with_retry` succeeds on first attempt when the callable returns: result is `Submitted(payload=..., attempt_count=1)`.
- [ ] `submit_with_retry` retries on transient errors and returns `Submitted` once the callable succeeds; `attempt_count` reflects the number of attempts made.
- [ ] `submit_with_retry` returns `GatewaySubmissionFailed` after exhausting `window_seconds` of wall-clock time on persistent transient errors; `last_error_class` carries the final exception's class name.
- [ ] `submit_with_retry` re-raises (does NOT return `GatewaySubmissionFailed`) when the callable raises a permanent rejection — it is the caller's responsibility to translate to a synchronous OMS rejection.
- [ ] `classify_alpaca_error` returns a `PermanentRejection` with the documented `code` for each of: `422 validation_failed`, `403 insufficient_buying_power`, `403 insufficient_shares`, `403 options_level_not_approved`, `422 contract_expired`, `403 underlying_halted`, `422 invalid_legs`. Other 4xx fall through to `code="other_permanent"`.
- [ ] `classify_alpaca_error` returns `None` for network errors (e.g., `httpx.ConnectError`), timeouts, and 5xx responses.
- [ ] `is_transient` matches `classify_alpaca_error(...) is None` for the same set of inputs.
- [ ] Every test in `tests/execution/broker_adapter/test_client_factory.py`, `test_retry.py`, `test_errors.py` passes under `uv run pytest tests/execution/broker_adapter -n auto`.
- [ ] `scripts/RUNBOOK_broker_adapter.md` exists with a stub structure (title, prerequisites placeholder, "Story 05 will populate this runbook" note).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/broker_adapter -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite remains green; no regressions in OMS / state-persistence / config-layer tests.
* Spot-check by `python -c "from alphamind.execution.broker_adapter import AlpacaClientFactory, submit_with_retry, classify_alpaca_error; print('ok')"` succeeds.
* Spot-check the RUNBOOK shell exists at `scripts/RUNBOOK_broker_adapter.md` and references story 05.
* Lint clean per CLAUDE.md.
