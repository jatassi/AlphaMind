"""add command-center-owned tables (alerts, webauthn_credentials, operator_sessions)

Revision ID: cc1f2e3d4a5b
Revises: a2c4e6f8b1d3
Create Date: 2026-05-26 04:00:00.000000

Adds the three command-center-owned tables per story 02 (ALP-666) — the
foundation every subsequent command-center story (03 auth, 04* control
proxy + SSE, 05a alert engine) writes to. The tables live on a separate
SQLAlchemy ``DeclarativeBase`` from the production
``alphamind.persistence.models.Base`` (see
``src/alphamind/command_center/persistence/tables.py``) so the
dual-session-factory split (cc_writer scoped to these three tables;
foreign_reader read-only against everything else) is structurally
enforced at the ORM mapper layer.

Chains off ``a2c4e6f8b1d3`` (the merge migration that resolved the
Wave 1 forked head — ALP-663 PROFILE_SWITCHED + ALP-665 monitor halt
mode landed in parallel) so the migration graph stays linear.

Per the parent issue's pre-resolved scope (§ Out of scope, F-group
tables), this migration does NOT add ``agent_calls`` / ``validations`` /
``validation_outcomes`` / ``retrospective_reports`` /
``retrospective_decisions`` / ``weekly_digest_snapshots`` /
``saved_queries`` — those defer to the feedback-loop follow-on tree.

Table shapes mirror the SQLAlchemy declarations in
``src/alphamind/command_center/persistence/tables.py``. The migration
test pins the column set; this docstring documents the column intent.

* ``alerts`` — populated by story 05a's alert engine. One row per fired
  rule. ``rule_name`` is the YAML-configured rule key from
  ``config/alerts.yaml``; ``severity`` and ``status`` carry the
  AlertSeverity / AlertStatus vocabularies (validated at the codec
  boundary, not at the schema layer — the design treats these as
  evolving vocabularies the dashboard surface owns, and a future rule
  with a novel severity tier should land via a coordinated
  schema-and-code update, not a silent insert).
* ``webauthn_credentials`` — populated by story 03's registration flow.
  ``credential_id`` is the base64url-encoded WebAuthn credential id;
  ``public_key`` is the CBOR-encoded public key as base64url;
  ``sign_count`` is the WebAuthn replay-protection counter;
  ``transports`` is a comma-separated transport hint list.
* ``operator_sessions`` — populated by story 03's session-issuance flow.
  ``credential_id`` FK targets ``webauthn_credentials.credential_id``
  with ``ON DELETE RESTRICT`` so an active session pins its credential
  row.

No CHECK constraints on the enum-typed columns — the codecs validate
the vocabularies at the boundary. Adding CHECKs here would harden the
contract; the parent issue defers that to a later cleanup pass once
the package shape is fixed (pre-resolved O — no new import-linter
contracts; same conservative posture for schema CHECKs).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cc1f2e3d4a5b"
down_revision: str | Sequence[str] | None = "a2c4e6f8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the three command-center-owned tables."""
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.Text(), nullable=False),
        sa.Column("rule_name", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("fired_at", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Text(), nullable=True),
        sa.Column("snoozed_until", sa.Text(), nullable=True),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("alert_id"),
    )

    op.create_table(
        "webauthn_credentials",
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("transports", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("credential_id"),
    )

    op.create_table(
        "operator_sessions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("csrf_token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["webauthn_credentials.credential_id"],
            name="fk_operator_sessions_credential_id",
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    """Drop the three command-center-owned tables.

    Drop order is FK-reverse: ``operator_sessions`` first (depends on
    ``webauthn_credentials``), then the two independent tables.
    """
    op.drop_table("operator_sessions")
    op.drop_table("webauthn_credentials")
    op.drop_table("alerts")
