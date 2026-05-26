"""Typed identifier aliases for the command-center package (story 02 / ALP-666).

Mirrors the convention in :mod:`alphamind._kernel.ids`: each alias is a
distinct :class:`typing.NewType` for the type checker but a plain ``str``
at runtime. Boundary-validating constructors (``webauthn_credential_id`` /
``discord_webhook_url`` / etc.) parse-don't-validate (python-architecture
§D2) so downstream consumers trust the typed value without re-checking.

Bare type aliases stay public for tests and fixtures that want to construct
known-good values without re-running validation.

The aliases declared here are NOT placed in the global ``alphamind._kernel``
package because they exist only inside the command-center bounded context —
``alphamind._kernel/ids.py`` carries identifiers that cross package
boundaries (``Symbol``, ``OrderId``, etc.). The ``import-linter`` ``kernel-leaf``
contract treats ``command_center`` as a forbidden upward dependency for the
global ``_kernel`` package; this local ``_kernel`` keeps the same naming
discipline within the package's own bounded context.
"""

from __future__ import annotations

import re
from typing import NewType

__all__ = [
    "AlertId",
    "AlertRuleName",
    "DiscordWebhookUrl",
    "OperatorSessionId",
    "WebauthnCredentialId",
    "alert_id",
    "alert_rule_name",
    "discord_webhook_url",
    "operator_session_id",
    "webauthn_credential_id",
]


OperatorSessionId = NewType("OperatorSessionId", str)
WebauthnCredentialId = NewType("WebauthnCredentialId", str)
AlertId = NewType("AlertId", str)
AlertRuleName = NewType("AlertRuleName", str)
DiscordWebhookUrl = NewType("DiscordWebhookUrl", str)


# ---------------------------------------------------------------------------
# Patterns.
# ---------------------------------------------------------------------------

_WEBAUTHN_CREDENTIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
"""base64url alphabet — A-Z, a-z, 0-9, ``-``, ``_``; no padding.

``py_webauthn`` serializes credential IDs as base64url-encoded strings (no
``=`` padding) so the credential row's PK can be a plain TEXT column.
Matching the alphabet here lets the boundary constructor reject standard
base64 (``+`` / ``/`` / ``=``) which would otherwise round-trip through
the DB unchanged but break credential lookup later.
"""

_DISCORD_WEBHOOK_HOSTS = frozenset({"discord.com", "discordapp.com"})
"""Acceptable Discord webhook hostnames.

``discordapp.com`` is the legacy host; Discord still serves webhook posts
against it. Both are accepted so an operator who pastes either form into
``config/alerts.yaml`` succeeds. Used by :func:`discord_webhook_url` to
validate the host after the URL pattern matches — keeps the host
vocabulary in one place rather than baking it into the regex (F12).
"""

_DISCORD_WEBHOOK_URL_PATTERN = re.compile(
    r"^https://(?P<host>[A-Za-z0-9.-]+)/api/webhooks/(?P<id>[0-9]+)/(?P<token>[A-Za-z0-9_-]+)$"
)
"""Discord webhook URL: ``https://{host}/api/webhooks/{id}/{token}``.

Pattern intentionally restrictive: ``https://`` only (Discord rejects HTTP
webhooks); ID is digits; token is base64url-compatible alphabet. Rejects
URLs with query strings / fragments since neither is part of the webhook
contract. The host is matched as a wildcard here and then validated against
:data:`_DISCORD_WEBHOOK_HOSTS` in :func:`discord_webhook_url` — keeps the
host vocabulary in one place (F12).
"""


# ---------------------------------------------------------------------------
# Boundary-validating constructors.
# ---------------------------------------------------------------------------


def webauthn_credential_id(value: str) -> WebauthnCredentialId:
    """Construct a :class:`WebauthnCredentialId`, validating the base64url alphabet.

    Raises :class:`ValueError` on empty input, padding characters, ``+`` / ``/``
    (standard base64), whitespace, or any character outside the base64url set.
    """
    if not value or not _WEBAUTHN_CREDENTIAL_ID_PATTERN.fullmatch(value):
        msg = (
            f"webauthn_credential_id must match {_WEBAUTHN_CREDENTIAL_ID_PATTERN.pattern!r} "
            f"(base64url; no padding); got {value!r}"
        )
        raise ValueError(msg)
    return WebauthnCredentialId(value)


def discord_webhook_url(value: str) -> DiscordWebhookUrl:
    """Construct a :class:`DiscordWebhookUrl`, validating the Discord URL shape.

    Accepts ``https://discord.com/api/webhooks/{id}/{token}`` or the legacy
    ``https://discordapp.com/api/webhooks/{id}/{token}``. Raises
    :class:`ValueError` on any other scheme, host, or path shape.

    Two-step validation (F12): the regex pins scheme + path shape; the
    parsed host is then checked against :data:`_DISCORD_WEBHOOK_HOSTS`
    so the accepted-host vocabulary stays in one place.
    """
    match = _DISCORD_WEBHOOK_URL_PATTERN.fullmatch(value)
    if match is None:
        msg = (
            f"discord_webhook_url must match {_DISCORD_WEBHOOK_URL_PATTERN.pattern!r}; "
            f"got {value!r}"
        )
        raise ValueError(msg)
    host = match.group("host")
    if host not in _DISCORD_WEBHOOK_HOSTS:
        allowed = sorted(_DISCORD_WEBHOOK_HOSTS)
        msg = (
            f"discord_webhook_url host {host!r} not accepted; allowed hosts: {allowed}"
        )
        raise ValueError(msg)
    return DiscordWebhookUrl(value)


def operator_session_id(value: str) -> OperatorSessionId:
    """Construct an :class:`OperatorSessionId`. Rejects empty input only.

    Session IDs are generated locally by the session-issue path (story 03)
    using ``secrets.token_urlsafe``; this constructor is the boundary
    validator for inputs read out of the DB or supplied by callers. No
    pattern beyond non-empty — the session-issue path picks the shape.
    """
    if not value:
        msg = f"operator_session_id must be non-empty; got {value!r}"
        raise ValueError(msg)
    return OperatorSessionId(value)


def alert_id(value: str) -> AlertId:
    """Construct an :class:`AlertId`. Rejects empty input only.

    Alert IDs are generated by the alert-engine path (story 05a) using the
    UUID-like form ``alert-{YYYY-MM-DD}-{token}``; this constructor is the
    boundary validator for inputs read out of the DB or supplied by callers.
    """
    if not value:
        msg = f"alert_id must be non-empty; got {value!r}"
        raise ValueError(msg)
    return AlertId(value)


def alert_rule_name(value: str) -> AlertRuleName:
    """Construct an :class:`AlertRuleName`. Rejects empty input only.

    Rule names come from the operator-edited ``config/alerts.yaml`` (story
    05a populates the default 17 rules); the YAML schema validates the
    full vocabulary. This constructor is the boundary on the DB read path.
    """
    if not value:
        msg = f"alert_rule_name must be non-empty; got {value!r}"
        raise ValueError(msg)
    return AlertRuleName(value)
