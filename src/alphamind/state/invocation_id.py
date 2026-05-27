"""Invocation-id minting (ALP-715 review F5).

Single source of truth for the ``inv-YYYYMMDDTHHMMSSZ-<8hex>`` shape every
producer of an ``invocations`` row writes — the scheduler's pipeline
invocations, the continuous monitor's borrow-accrual ticks, and the
command center's operator-action rows. The timestamp prefix is the
archive-sort key, so the format must agree across all three producers.

Lives in :mod:`alphamind.state` (the import-linter "Composition roots
sit above the domain" contract forbids ``execution.*`` from importing
``scheduler.*``; siting the helper in the cross-cutting ``state``
package keeps it below both layers).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

__all__ = ["mint_invocation_id"]


def mint_invocation_id(now: datetime) -> str:
    """Mint a fresh invocation id: ``inv-YYYYMMDDTHHMMSSZ-<8-hex>``.

    The timestamp prefix sorts lexicographically — critical for the
    ``_persist_data_calibration_snapshot`` directory scan that picks the
    most recent prior invocation. The 8-hex suffix is drawn from
    ``secrets.token_hex`` so concurrent invocations within the same
    second do not collide.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"inv-{stamp}-{secrets.token_hex(4)}"
