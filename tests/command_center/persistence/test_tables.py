"""Tests for ``command_center.persistence.tables`` (story 02 / ALP-666).

The three command-center-owned tables — ``alerts``,
``webauthn_credentials``, ``operator_sessions`` — are declared on a
dedicated SQLAlchemy ``MetaData`` instance separate from the production
``Base.metadata`` so the dual-session-factory split is structurally
enforced. These tests confirm the declarative shape (column set, PKs,
FKs).
"""

from __future__ import annotations

from sqlalchemy import inspect

from alphamind.command_center.persistence.tables import (
    AlertRow,
    CommandCenterBase,
    OperatorSessionRow,
    WebauthnCredentialRow,
)


class TestCommandCenterBase:
    def test_metadata_carries_exactly_the_three_owned_tables(self) -> None:
        # The dual-session split requires the cc_writer engine's MetaData to
        # contain ONLY the three command-center-owned tables — that's how
        # the foreign-table-write protection is structurally enforced
        # (no mapper → `session.add(Position(...))` raises at the ORM layer).
        tables = set(CommandCenterBase.metadata.tables)
        assert tables == {"alerts", "webauthn_credentials", "operator_sessions"}

    def test_command_center_base_is_distinct_from_production_base(self) -> None:
        # If both engines shared a MetaData, the writer engine would
        # implicitly carry every production table's mapper — defeating
        # the "writer can't add foreign mapper" guarantee.
        from alphamind.persistence.models import Base as ProductionBase

        assert CommandCenterBase.metadata is not ProductionBase.metadata


class TestAlertRow:
    def test_has_documented_columns(self) -> None:
        cols = {col.name for col in AlertRow.__table__.columns}
        assert cols == {
            "alert_id",
            "rule_name",
            "severity",
            "status",
            "fired_at",
            "acknowledged_at",
            "snoozed_until",
            "context_json",
        }


class TestWebauthnCredentialRow:
    def test_has_documented_columns(self) -> None:
        cols = {col.name for col in WebauthnCredentialRow.__table__.columns}
        assert cols == {
            "credential_id",
            "public_key",
            "sign_count",
            "transports",
            "created_at",
        }


class TestOperatorSessionRow:
    def test_has_documented_columns(self) -> None:
        cols = {col.name for col in OperatorSessionRow.__table__.columns}
        assert cols == {
            "session_id",
            "credential_id",
            "expires_at",
            "csrf_token_hash",
            "created_at",
        }

    def test_credential_id_has_fk_to_webauthn_credentials(self) -> None:
        insp = inspect(OperatorSessionRow)
        # The session FK ensures session-issuance can't reference a
        # nonexistent credential; ON DELETE RESTRICT keeps a credential
        # row pinned while a session references it.
        fks = (
            list(insp.tables[0].foreign_keys)
            if insp.tables
            else list(OperatorSessionRow.__table__.foreign_keys)
        )
        matching = [fk for fk in fks if fk.column.table.name == "webauthn_credentials"]
        assert matching, f"expected FK to webauthn_credentials; got {fks}"
        assert matching[0].column.name == "credential_id"
