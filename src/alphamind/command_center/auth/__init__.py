"""Auth surface for the command center.

Single-operator WebAuthn (passkey) authentication with signed-cookie
sessions and double-submit CSRF protection.

Public re-exports for downstream consumers:

* :class:`WebauthnVerifier` — Protocol the route layer consumes.
* :class:`InMemoryWebauthnVerifier` — deterministic fake for unit tests.
* :class:`RealWebauthnVerifier` — wraps ``py_webauthn`` (the py-webauthn
  translation seam is unit-tested; full ceremonies with real authenticators
  are the verify script's responsibility, story 07).
* :func:`current_session` — FastAPI ``Depends`` returning the validated
  :class:`OperatorSessionId` for the presented session cookie. Raises
  ``HTTPException(401)`` on any rejection path.
* :func:`csrf_required` — FastAPI ``Depends`` enforcing the double-
  submit CSRF check on state-mutating verbs. Raises
  ``HTTPException(403)`` on any rejection path.
* :func:`build_auth_router` — builds the ``/auth/*`` router for
  ``app.include_router``.
* :class:`SetupTokenGate` — first-launch enrollment gate.

**Contract for downstream stories.** Every ``/api/*`` router added by
later stories MUST:

* Include ``Depends(current_session)`` on every endpoint (the route is
  gated behind a valid session).
* Include ``Depends(csrf_required)`` on every state-mutating verb
  (POST / PUT / PATCH / DELETE). Read-only endpoints (GET / HEAD) do
  not require CSRF.

Document this contract on the router's docstring so the next consumer
finds it without re-reading this file.
"""

from __future__ import annotations

from alphamind.command_center.auth.dependencies import (
    csrf_required,
    current_session,
)
from alphamind.command_center.auth.routes import build_auth_router
from alphamind.command_center.auth.setup_token import (
    SetupTokenAlreadyConsumedError,
    SetupTokenGate,
    SetupTokenMismatchError,
)
from alphamind.command_center.auth.webauthn import (
    InMemoryWebauthnVerifier,
    RealWebauthnVerifier,
    WebauthnVerifier,
)

__all__ = [
    "InMemoryWebauthnVerifier",
    "RealWebauthnVerifier",
    "SetupTokenAlreadyConsumedError",
    "SetupTokenGate",
    "SetupTokenMismatchError",
    "WebauthnVerifier",
    "build_auth_router",
    "csrf_required",
    "current_session",
]
