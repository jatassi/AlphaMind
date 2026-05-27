"""Tests for :mod:`alphamind.command_center.alerts.persistence` (story 05a / ALP-671)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import alert_id, alert_rule_name
from alphamind.command_center.alerts.persistence import (
    insert_fired,
    list_active,
    load_alert,
    mint_alert_id,
    update_acknowledged,
    update_snoozed,
)
from alphamind.command_center.persistence.codecs import AlertSeverity, AlertStatus

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


class TestMintAlertId:
    def test_id_includes_date_prefix(self) -> None:
        new_id = mint_alert_id(_NOW)
        assert str(new_id).startswith("alert-2026-05-26-")

    def test_id_includes_random_suffix(self) -> None:
        a = mint_alert_id(_NOW)
        b = mint_alert_id(_NOW)
        assert a != b


class TestInsertFired:
    @pytest.mark.asyncio
    async def test_inserts_firing_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            context_json='{"foo": "bar"}',
            fired_at=_NOW,
        )
        record = await load_alert(cc_writer_factory, alert_id_=new_id)
        assert record is not None
        assert record.status == AlertStatus.FIRING
        assert record.severity == AlertSeverity.CRITICAL
        assert record.rule_name == alert_rule_name("test_rule")
        assert record.context_json == '{"foo": "bar"}'
        assert record.acknowledged_at is None
        assert record.snoozed_until is None


class TestUpdateAcknowledged:
    @pytest.mark.asyncio
    async def test_acknowledges_firing_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        ack_time = _NOW + timedelta(minutes=1)
        ok = await update_acknowledged(
            cc_writer_factory, alert_id_=new_id, acknowledged_at=ack_time
        )
        assert ok is True
        record = await load_alert(cc_writer_factory, alert_id_=new_id)
        assert record is not None
        assert record.status == AlertStatus.ACKNOWLEDGED
        assert record.acknowledged_at is not None

    @pytest.mark.asyncio
    async def test_double_ack_returns_false(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        await update_acknowledged(cc_writer_factory, alert_id_=new_id, acknowledged_at=_NOW)
        second = await update_acknowledged(
            cc_writer_factory, alert_id_=new_id, acknowledged_at=_NOW
        )
        assert second is False

    @pytest.mark.asyncio
    async def test_missing_id_returns_false(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ok = await update_acknowledged(
            cc_writer_factory,
            alert_id_=alert_id("alert-missing"),
            acknowledged_at=_NOW,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_acknowledges_snoozed_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Regression for finding #7 (Wave-5 review).

        The previous filter accepted only ``status = 'firing'`` so an
        operator-facing acknowledge action against a snoozed row was a
        silent no-op (the row stayed snoozed, the route returned 200
        with no audit entry). Snooze → ack is a legitimate transition;
        widen the filter to accept both pre-ack states.
        """
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        await update_snoozed(
            cc_writer_factory, alert_id_=new_id, snoozed_until=_NOW + timedelta(hours=1)
        )
        ok = await update_acknowledged(cc_writer_factory, alert_id_=new_id, acknowledged_at=_NOW)
        assert ok is True
        record = await load_alert(cc_writer_factory, alert_id_=new_id)
        assert record is not None
        assert record.status == AlertStatus.ACKNOWLEDGED


class TestUpdateSnoozed:
    @pytest.mark.asyncio
    async def test_snoozes_firing_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        snooze_until = _NOW + timedelta(hours=1)
        ok = await update_snoozed(cc_writer_factory, alert_id_=new_id, snoozed_until=snooze_until)
        assert ok is True
        record = await load_alert(cc_writer_factory, alert_id_=new_id)
        assert record is not None
        assert record.status == AlertStatus.SNOOZED
        assert record.snoozed_until is not None

    @pytest.mark.asyncio
    async def test_can_extend_snooze(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        await update_snoozed(
            cc_writer_factory, alert_id_=new_id, snoozed_until=_NOW + timedelta(minutes=15)
        )
        ok = await update_snoozed(
            cc_writer_factory, alert_id_=new_id, snoozed_until=_NOW + timedelta(hours=1)
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_cannot_snooze_acked_row(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        await update_acknowledged(cc_writer_factory, alert_id_=new_id, acknowledged_at=_NOW)
        ok = await update_snoozed(
            cc_writer_factory, alert_id_=new_id, snoozed_until=_NOW + timedelta(hours=1)
        )
        assert ok is False


class TestListActive:
    @pytest.mark.asyncio
    async def test_returns_firing_rows(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        a_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("rule_a"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        b_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("rule_b"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW + timedelta(minutes=5),
        )
        active = await list_active(cc_writer_factory, now=_NOW + timedelta(minutes=10))
        ids = [r.alert_id for r in active]
        assert a_id in ids
        assert b_id in ids

    @pytest.mark.asyncio
    async def test_excludes_acked(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("rule_x"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        await update_acknowledged(cc_writer_factory, alert_id_=new_id, acknowledged_at=_NOW)
        active = await list_active(cc_writer_factory, now=_NOW + timedelta(minutes=1))
        assert all(r.alert_id != new_id for r in active)

    @pytest.mark.asyncio
    async def test_includes_snoozed_in_window(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("rule_y"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        await update_snoozed(
            cc_writer_factory,
            alert_id_=new_id,
            snoozed_until=_NOW + timedelta(hours=1),
        )
        active = await list_active(cc_writer_factory, now=_NOW + timedelta(minutes=30))
        assert any(r.alert_id == new_id for r in active)

    @pytest.mark.asyncio
    async def test_excludes_expired_snooze(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("rule_z"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        await update_snoozed(
            cc_writer_factory,
            alert_id_=new_id,
            snoozed_until=_NOW + timedelta(minutes=10),
        )
        # Advance past snooze.
        active = await list_active(cc_writer_factory, now=_NOW + timedelta(hours=1))
        assert all(r.alert_id != new_id for r in active)


class TestLoadAlert:
    @pytest.mark.asyncio
    async def test_returns_none_for_missing(
        self,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        result = await load_alert(cc_writer_factory, alert_id_=alert_id("alert-missing"))
        assert result is None
