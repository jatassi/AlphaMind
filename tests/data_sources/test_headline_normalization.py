"""Tests for HeadlineType promotion and normalize_vendor_tags loader.

Story ALP-189 parts A and B — promotes the design-time HeadlineType taxonomy
to production code and adds the collector-boundary normalization helper that
the per-vendor news adapters call before persisting topic_tags.
"""

from __future__ import annotations

from alphamind.data_sources._common import HeadlineType, normalize_vendor_tags


class TestHeadlineTypeEnum:
    """Part A — HeadlineType is promoted from the design schema to production."""

    def test_has_fourteen_canonical_members(self) -> None:
        expected = {
            "breaking",
            "earnings_related",
            "m_and_a",
            "analyst_action",
            "regulatory",
            "geopolitical",
            "macro_data",
            "insider_activity",
            "short_report",
            "activist",
            "product_launch",
            "supply_chain",
            "guidance",
            "sector_rotation",
        }
        actual = {member.value for member in HeadlineType}
        assert actual == expected

    def test_is_strenum(self) -> None:
        # StrEnum members serialize as their string value, which is the
        # property downstream adapters rely on when persisting topic_tags.
        assert HeadlineType.EARNINGS_RELATED.value == "earnings_related"
        assert isinstance(HeadlineType.EARNINGS_RELATED, str)

    def test_value_round_trips_through_constructor(self) -> None:
        assert HeadlineType("macro_data") is HeadlineType.MACRO_DATA


class TestNormalizeVendorTags:
    """Part B — normalize vendor-raw tags into canonical HeadlineType values."""

    def test_marketaux_maps_known_topics(self) -> None:
        result = normalize_vendor_tags("marketaux", ["earnings", "guidance"])
        assert result == [HeadlineType.EARNINGS_RELATED, HeadlineType.GUIDANCE]

    def test_marketaux_drops_unmapped_silently(self) -> None:
        result = normalize_vendor_tags(
            "marketaux", ["earnings", "guidance", "completely_unknown_tag"]
        )
        assert result == [HeadlineType.EARNINGS_RELATED, HeadlineType.GUIDANCE]

    def test_sec_edgar_8k_maps_item_codes(self) -> None:
        result = normalize_vendor_tags("sec_edgar_8k", ["2.02", "5.02", "9.99"])
        assert result == [HeadlineType.EARNINGS_RELATED, HeadlineType.INSIDER_ACTIVITY]

    def test_finnhub_vendor_key_resolves(self) -> None:
        # The mapping yaml documents finnhub keys; the loader must expose them.
        result = normalize_vendor_tags("finnhub", ["merger", "earnings"])
        assert result == [HeadlineType.M_AND_A, HeadlineType.EARNINGS_RELATED]

    def test_rss_topic_vendor_key_resolves(self) -> None:
        result = normalize_vendor_tags("rss_topic", ["fda", "tariff"])
        assert result == [HeadlineType.REGULATORY, HeadlineType.GEOPOLITICAL]

    def test_unknown_vendor_returns_empty(self) -> None:
        # An unknown vendor key should drop all tags (no mapping table).
        assert normalize_vendor_tags("nonexistent_vendor", ["anything"]) == []

    def test_empty_input_returns_empty(self) -> None:
        assert normalize_vendor_tags("marketaux", []) == []
