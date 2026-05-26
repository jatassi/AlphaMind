"""SQLAlchemy declarative tables for the three command-center-owned tables.

The dual-session-factory split (story 02 / ALP-666 pre-resolved H) is
*structurally* enforced: the ``cc_writer`` engine binds to a SQLAlchemy
:class:`~sqlalchemy.orm.DeclarativeBase` whose :class:`~sqlalchemy.MetaData`
contains ONLY the three tables declared here, while the foreign tables
(``invocations`` / ``activity_log`` / ``positions`` / ``orders`` / ...) live
on the production :data:`alphamind.persistence.models.Base.metadata`.

The writer engine therefore has no mapper for any foreign table, so a
caller's ``session.add(Position(...))`` raises at the ORM layer ("Class is
not mapped") before reaching the SQLite layer. The foreign-table-read
path (``foreign_reader``) connects to the same DB with ``?mode=ro`` in the
URI; foreign-table read access is via core SQL or reflected metadata, and
write attempts raise at the SQLite layer ("attempt to write a readonly
database").

Three tables:

* :class:`AlertRow` — populated by story 05a (alert engine).
* :class:`WebauthnCredentialRow` — populated by story 03 (auth).
* :class:`OperatorSessionRow` — populated by story 03 (auth);
  FKs into ``webauthn_credentials``.

Mirrors the column shape pinned in :doc:`docs/design/command-center.md`
§ Persistence boundary and the story-02 scope.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class CommandCenterBase(DeclarativeBase):
    """Declarative base for the three command-center-owned tables.

    Separate from :data:`alphamind.persistence.models.Base` so the
    command-center writer engine's :class:`~sqlalchemy.MetaData` contains
    ONLY the three owned tables — the structural enforcement that makes
    ``session.add(Position(...))`` raise at the ORM layer.
    """


class AlertRow(CommandCenterBase):
    """One row per fired alert.

    Populated by story 05a (alert engine). The engine evaluates the rule
    set on each SSE-event tick + a 60s periodic fallback (per parent-issue
    pre-resolved F); a fired rule whose previous evaluation was clear
    inserts one row here.

    The ``context_json`` column carries the per-rule payload (e.g., for
    ``critical_api_failure`` the failing category + most-recent
    invocation_id) as a JSON-encoded string so the dashboard's
    in-app banner has the data it needs to render the click-through.

    Indexes intentionally absent at story 02 — story 05a will add the
    indexes the alert dashboard's queries need (``rule_name + fired_at``
    for the per-rule history view, ``status`` for the active-alerts
    fetch). Adding them now is YAGNI.
    """

    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rule_name: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    fired_at: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    snoozed_until: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False)


class WebauthnCredentialRow(CommandCenterBase):
    """One row per registered WebAuthn passkey.

    Populated by story 03 (WebAuthn registration). The relying-party logic
    in ``py_webauthn`` produces the credential ID as a base64url-encoded
    string + the public key as a CBOR-encoded byte string — the row carries
    them as TEXT and TEXT (base64url-encoded) respectively so the storage
    layer stays driver-agnostic.

    ``sign_count`` is the WebAuthn replay-protection counter the relying
    party increments on each successful assertion; ``transports`` is a
    comma-separated list of declared transport hints (``usb`` / ``nfc`` /
    ``ble`` / ``internal``) the registration ceremony reported.

    ``created_at`` lets the operator inspect their registered credentials
    + revoke stale ones from the auth-management view (story 03's surface).
    """

    __tablename__ = "webauthn_credentials"

    credential_id: Mapped[str] = mapped_column(Text, primary_key=True)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False)
    transports: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class OperatorSessionRow(CommandCenterBase):
    """One row per active operator session.

    Populated by story 03 (session issuance). Each session is issued for
    a successful WebAuthn assertion; the row carries the FK back to the
    credential that asserted, the CSRF token hash (the signed cookie
    carries the raw token; the DB carries the hash so a DB compromise
    doesn't yield session tokens directly), and the expiration timestamp.

    ``ON DELETE RESTRICT`` on the credential FK ensures the operator's
    auth-management view can't delete a credential while it still has an
    active session (the operator must log out the session first; the
    session-eviction surface lands in story 03).
    """

    __tablename__ = "operator_sessions"

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    credential_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "webauthn_credentials.credential_id",
            name="fk_operator_sessions_credential_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
