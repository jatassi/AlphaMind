"""Setup-token gate for first-launch operator enrollment (story 03 / ALP-667).

When the command center starts with zero rows in ``webauthn_credentials``,
the operator needs *some* way to enroll the first passkey. Without a
gate, anyone on the LAN could enroll themselves and lock the legitimate
operator out — the design doc § Authentication § first-launch flow
mandates a one-time setup token printed on the local console.

This module owns one in-process gate. The daemon mints a token at
startup, prints it to stdout (NSSM captures it in the log file); the
operator presents the token to ``/auth/register/begin``. The first
successful registration consumes the token and locks the gate.
Subsequent registrations route through the existing-session bearer
path; the gate refuses to mint or consume again.

The gate is in-process state (a singleton on ``app.state``); a daemon
restart re-mints a fresh token. That's intentional — the operator who
just installed the system reads the new token off the boot log.
"""

from __future__ import annotations

import hmac
import secrets

__all__ = [
    "SetupTokenAlreadyConsumed",
    "SetupTokenGate",
    "SetupTokenMismatch",
]


class SetupTokenMismatch(Exception):
    """Raised when :meth:`SetupTokenGate.consume` is called with a wrong token.

    The route layer catches this and returns ``403 Forbidden`` — the
    operator's response is identical to "gate already consumed" so a
    probing client gets no information about the gate's state.
    """


class SetupTokenAlreadyConsumed(Exception):
    """Raised when the gate is already consumed and another call is attempted.

    Both :meth:`SetupTokenGate.mint` and :meth:`SetupTokenGate.consume`
    raise this after the first successful consume — the route layer
    catches it on the consume path and returns ``403 Forbidden``.
    """


class SetupTokenGate:
    """One-time setup-token gate.

    Lifecycle: ``mint()`` → ``consume(token)`` → locked. After locked,
    both :meth:`mint` and :meth:`consume` raise
    :class:`SetupTokenAlreadyConsumed`.

    The gate is a per-process singleton on ``app.state``; a daemon
    restart re-mints. No persistence: the gate's state is derivable from
    ``count_credentials(cc_factory) == 0`` plus "did the operator already
    consume this process's token", and only the consume bit is
    interesting on a fresh boot.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._consumed = False

    def mint(self) -> str:
        """Mint a fresh setup token.

        Raises :class:`SetupTokenAlreadyConsumed` if the gate is locked.
        Returns a URL-safe random string the daemon prints to stdout for
        the operator to present at ``/auth/register/begin``.

        Calling :meth:`mint` again before :meth:`consume` is permitted —
        the most recent token is the canonical one. (A daemon that ran
        through its boot sequence twice might re-mint; that's harmless
        because only the most recent token can be consumed.)
        """
        if self._consumed:
            msg = "setup token already consumed; further enrollments require an existing session"
            raise SetupTokenAlreadyConsumed(msg)
        self._token = secrets.token_urlsafe(32)
        return self._token

    def consume(self, presented: str) -> None:
        """Consume the gate with *presented*; locks the gate on success.

        Raises :class:`SetupTokenMismatch` if *presented* does not match
        the most recently minted token. Raises
        :class:`SetupTokenAlreadyConsumed` if the gate is locked already.

        Constant-time string compare so a timing-attack measurement of
        "is this token close" doesn't yield information.
        """
        if self._consumed:
            msg = "setup token already consumed; further enrollments require an existing session"
            raise SetupTokenAlreadyConsumed(msg)
        if self._token is None or not hmac.compare_digest(self._token, presented):
            raise SetupTokenMismatch("setup token does not match")
        self._consumed = True
        self._token = None

    def is_consumed(self) -> bool:
        """Return ``True`` iff the gate has been consumed."""
        return self._consumed
