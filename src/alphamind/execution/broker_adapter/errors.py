"""Alpaca error classification (story 01 / ALP-378).

Maps ``alpaca.common.exceptions.APIError`` and network-level exceptions into
the broker adapter's typed rejection taxonomy. The classifier is consumed by
:func:`alphamind.execution.broker_adapter.retry.submit_with_retry` to decide
whether to retry (transient) or re-raise (permanent), and by order-submission
helpers to translate permanent rejections into synchronous OMS rejections per
``broker-adapter.md § Order submission``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

PermanentRejectionCode = Literal[
    "validation_failed",  # 422 generic
    "insufficient_buying_power",  # 403
    "insufficient_shares",  # 403 short / sell
    "options_level_not_approved",  # 403 options
    "contract_expired",  # 422 options
    "underlying_halted",  # 403 options
    "invalid_legs",  # 422 mleg
    "asset_not_tradable",  # 403 / 422 generic
    "other_permanent",  # fallback for unmapped 4xx
]


@dataclass(frozen=True)
class PermanentRejection:
    """Alpaca rejected the submission for a non-retriable reason.

    Surfaced to the OMS as a synchronous rejection — the caller does NOT
    re-enqueue the command. The PM may revise and resubmit on its next
    invocation.
    """

    code: PermanentRejectionCode
    http_status: int
    alpaca_message: str


def classify_alpaca_error(exc: Exception) -> PermanentRejection | None:
    """Return :class:`PermanentRejection` if *exc* is non-retriable; else ``None``.

    ``None`` routes the caller's retry loop to retry — network errors, timeouts,
    and 5xx responses fall here. ``APIError`` instances are inspected for HTTP
    status and Alpaca's error message; permanent 4xx rejections (validation
    failures, insufficient buying power, options-level mismatches, expired
    contracts, halted underlyings, invalid mleg structure, and untradable
    assets) return a :class:`PermanentRejection` carrying the documented
    :data:`PermanentRejectionCode`.
    """
    status = _http_status(exc)
    if status is None or not (400 <= status < 500):
        return None
    message = _alpaca_message(exc)
    return PermanentRejection(
        code=_classify_4xx(status, message),
        http_status=status,
        alpaca_message=message,
    )


def is_transient(exc: Exception) -> bool:
    """Default transient classifier for :func:`submit_with_retry`.

    Equivalent to ``classify_alpaca_error(exc) is None``.
    """
    return classify_alpaca_error(exc) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_status(exc: Exception) -> int | None:
    """Extract HTTP status from an exception, or ``None`` if unavailable.

    ``APIError`` exposes ``.status_code`` directly. Other exceptions
    (``httpx.ConnectError``, ``httpx.TimeoutException``, ``OSError``)
    have no HTTP status — the request never reached the server, so ``None``
    routes them down the transient path.
    """
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _alpaca_message(exc: Exception) -> str:
    """Extract Alpaca's error message text, falling back to ``str(exc)``.

    ``APIError.message`` is a property that decodes the JSON body; a malformed
    body raises ``json.JSONDecodeError``. ``KeyError`` covers responses that
    decode but lack a ``"message"`` key.
    """
    try:
        raw = getattr(exc, "message", None)
    except (json.JSONDecodeError, KeyError):
        raw = None
    return raw if isinstance(raw, str) else str(exc)


_FOUR_OH_THREE_RULES: tuple[tuple[tuple[str, ...], PermanentRejectionCode], ...] = (
    (("insufficient buying power", "buying_power"), "insufficient_buying_power"),
    (("insufficient shares", "insufficient_shares"), "insufficient_shares"),
    (
        ("options level", "options_level", "level_not_approved"),
        "options_level_not_approved",
    ),
    (("not tradable", "non-tradable", "non_tradable"), "asset_not_tradable"),
)

_FOUR_TWENTY_TWO_RULES: tuple[tuple[tuple[str, ...], PermanentRejectionCode], ...] = (
    (("contract expired", "expired"), "contract_expired"),
    (("invalid leg", "legs"), "invalid_legs"),
    (("validation", "invalid", "unprocessable"), "validation_failed"),
    (("not tradable", "non-tradable", "non_tradable"), "asset_not_tradable"),
)


def _classify_4xx(status: int, message: str) -> PermanentRejectionCode:
    """Map a 4xx (status, message) pair to a :data:`PermanentRejectionCode`.

    Alpaca surfaces semantic intent through the ``message`` text. Match
    documented rejection reasons in ``broker-adapter.md § Order submission``
    case-insensitively. The ``underlying_halted`` rule (403, options) requires
    both ``underlying`` AND ``halt`` keywords and is checked separately so the
    declarative match-table can stay any-of.
    """
    lowered = message.lower()
    if status == 403:
        if "underlying" in lowered and "halt" in lowered:
            return "underlying_halted"
        for keywords, code in _FOUR_OH_THREE_RULES:
            if any(k in lowered for k in keywords):
                return code
    if status == 422:
        for keywords, code in _FOUR_TWENTY_TWO_RULES:
            if any(k in lowered for k in keywords):
                return code
    return "other_permanent"
