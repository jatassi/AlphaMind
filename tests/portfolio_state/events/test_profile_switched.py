"""Tests for the PROFILE_SWITCHED event substrate (ALP-663).

Covers:
- EventType.PROFILE_SWITCHED exists and is in EventGroup.CONFIGURATION.
- ProfileSwitchedDetail frozen dataclass with correct fields.
- EVENT_TYPE_TO_DETAIL_CLASS dispatch returns ProfileSwitchedDetail.
- ProfileSwitchedDetail is exported from events/__init__ and activity_log shim.
- Codec round-trip: encode_detail + decode_detail is identity.
"""

from __future__ import annotations

import dataclasses

import pytest

from alphamind.config.models.main import Profile
from alphamind.portfolio_state.events import (
    EVENT_TYPE_TO_DETAIL_CLASS,
    EVENT_TYPE_TO_GROUP,
    EventGroup,
    EventType,
    decode_detail,
    encode_detail,
)


class TestEventTypeProfileSwitched:
    def test_profile_switched_member_exists(self) -> None:
        assert hasattr(EventType, "PROFILE_SWITCHED")
        assert EventType.PROFILE_SWITCHED.value == "PROFILE_SWITCHED"

    def test_profile_switched_in_configuration_group(self) -> None:
        assert EVENT_TYPE_TO_GROUP[EventType.PROFILE_SWITCHED] is EventGroup.CONFIGURATION


class TestProfileSwitchedDetailDataclass:
    def test_detail_class_is_registered(self) -> None:
        from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail

        assert EVENT_TYPE_TO_DETAIL_CLASS[EventType.PROFILE_SWITCHED] is ProfileSwitchedDetail

    def test_detail_is_frozen_dataclass(self) -> None:
        from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail

        assert dataclasses.is_dataclass(ProfileSwitchedDetail)
        # frozen=True means assignment raises FrozenInstanceError
        instance = ProfileSwitchedDetail(
            previous_profile=Profile.medium,
            new_profile=Profile.large,
            is_no_op=False,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.is_no_op = True  # type: ignore[misc]

    def test_detail_fields(self) -> None:
        from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail

        field_names = {f.name for f in dataclasses.fields(ProfileSwitchedDetail)}
        assert field_names == {"previous_profile", "new_profile", "is_no_op"}

    def test_is_no_op_true_when_same_profile(self) -> None:
        from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail

        d = ProfileSwitchedDetail(
            previous_profile=Profile.medium,
            new_profile=Profile.medium,
            is_no_op=True,
        )
        assert d.is_no_op is True
        assert d.previous_profile is d.new_profile

    def test_is_no_op_false_when_different_profile(self) -> None:
        from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail

        d = ProfileSwitchedDetail(
            previous_profile=Profile.small,
            new_profile=Profile.large,
            is_no_op=False,
        )
        assert d.is_no_op is False


class TestProfileSwitchedDetailExports:
    def test_exported_from_events_init(self) -> None:
        from alphamind.portfolio_state.events import ProfileSwitchedDetail  # noqa: F401

    def test_exported_from_activity_log_shim(self) -> None:
        from alphamind.portfolio_state.events.activity_log import (
            ProfileSwitchedDetail,  # noqa: F401
        )


class TestProfileSwitchedDetailCodecRoundTrip:
    def test_round_trip_is_identity(self) -> None:
        from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail

        original = ProfileSwitchedDetail(
            previous_profile=Profile.small,
            new_profile=Profile.large,
            is_no_op=False,
        )
        serialized = encode_detail(original)
        recovered = decode_detail(serialized, ProfileSwitchedDetail)
        assert recovered == original

    def test_round_trip_no_op_variant(self) -> None:
        from alphamind.portfolio_state.events.configuration import ProfileSwitchedDetail

        original = ProfileSwitchedDetail(
            previous_profile=Profile.medium,
            new_profile=Profile.medium,
            is_no_op=True,
        )
        serialized = encode_detail(original)
        recovered = decode_detail(serialized, ProfileSwitchedDetail)
        assert recovered == original
