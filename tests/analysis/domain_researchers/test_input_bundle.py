"""Tests for per-sector input bundle assembler (ALP-188)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.input_bundle import (
    InputBundle,
    assemble_input_bundle,
)
from alphamind.analysis.domain_researchers.qualitative_input import (
    EventEntry,
    HeadlineEntry,
    SectorQualitativeInput,
)
from alphamind.data_sources._common import HeadlineType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_AS_OF = datetime(2024, 3, 15, 14, 30, 0, tzinfo=UTC)
_DATA_FRESHNESS = datetime(2024, 3, 15, 14, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "inv-test-001"
_DISTILLATION_TEXT = """\
=== SECTOR TECH SEMIS ===
Some distillation content here.
=== UNIVERSAL CONTEXT ===
Market regime: BULL_TRENDING
"""


def _make_headline(
    headline: str = "NVDA beats estimates",
    source_outlet: str = "Bloomberg",
    tier: str = "tier_1",
    published_at: datetime | None = None,
    tickers: tuple[str, ...] = ("NVDA",),
    tags: tuple[HeadlineType, ...] = (HeadlineType.EARNINGS_RELATED,),
) -> HeadlineEntry:
    return HeadlineEntry(
        headline=headline,
        source_outlet=source_outlet,
        tier=tier,  # type: ignore[arg-type]
        published_at=published_at or _AS_OF,
        tickers=tickers,
        tags=tags,
    )


def _make_event(
    event_id: str = "evt-001",
    event_name: str = "NVDA Earnings Call",
    event_time: datetime | None = None,
    sectors: frozenset[Sector] | None = None,
    tickers: tuple[str, ...] = ("NVDA",),
    consensus: str | None = "EPS $5.16, revenue $22.1B",
) -> EventEntry:
    return EventEntry(
        event_id=event_id,
        event_name=event_name,
        event_time=event_time or _AS_OF,
        sectors=sectors if sectors is not None else frozenset({Sector.TECH_SEMIS}),
        tickers=tickers,
        consensus=consensus,
    )


def _make_qualitative(
    sector: Sector = Sector.TECH_SEMIS,
    headlines: tuple[HeadlineEntry, ...] = (),
    events: tuple[EventEntry, ...] = (),
    lookback_window_hours: int = 24,
) -> SectorQualitativeInput:
    return SectorQualitativeInput(
        sector=sector,
        as_of=_AS_OF,
        lookback_window_hours=lookback_window_hours,
        headlines=headlines,
        events=events,
        data_freshness=_DATA_FRESHNESS,
    )


def _assemble_default(
    *,
    sector: Sector = Sector.TECH_SEMIS,
    invocation_id: str = _INVOCATION_ID,
    as_of: datetime = _AS_OF,
    distillation_text: str = _DISTILLATION_TEXT,
    qualitative_input: SectorQualitativeInput | None = None,
) -> InputBundle:
    return assemble_input_bundle(
        sector=sector,
        invocation_id=invocation_id,
        as_of=as_of,
        distillation_text=distillation_text,
        qualitative_input=qualitative_input or _make_qualitative(),
    )


# ---------------------------------------------------------------------------
# Test 1: InputBundle is a frozen Pydantic model with the correct fields
# ---------------------------------------------------------------------------


def test_input_bundle_is_frozen_pydantic_model() -> None:
    """InputBundle is a BaseModel subclass and frozen."""
    assert issubclass(InputBundle, BaseModel)
    assert InputBundle.model_config.get("frozen") is True


def test_input_bundle_has_required_fields() -> None:
    """InputBundle exposes the six documented fields."""
    fields = InputBundle.model_fields
    assert "sector" in fields
    assert "invocation_id" in fields
    assert "as_of" in fields
    assert "distillation_text" in fields
    assert "qualitative_text" in fields
    assert "bundle_text" in fields


def test_input_bundle_is_immutable() -> None:
    """Frozen model raises ValidationError on attribute mutation."""
    bundle = _assemble_default()
    with pytest.raises(ValidationError):
        bundle.invocation_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 2: assemble_input_bundle returns an InputBundle
# ---------------------------------------------------------------------------


def test_assemble_returns_input_bundle() -> None:
    """assemble_input_bundle returns an InputBundle instance."""
    bundle = _assemble_default()
    assert isinstance(bundle, InputBundle)


def test_assemble_fields_match_inputs() -> None:
    """Returned bundle has sector, invocation_id, and as_of matching inputs."""
    bundle = _assemble_default()
    assert bundle.sector == Sector.TECH_SEMIS
    assert bundle.invocation_id == _INVOCATION_ID
    assert bundle.as_of == _AS_OF


# ---------------------------------------------------------------------------
# Test 3: H1 header
# ---------------------------------------------------------------------------


def test_bundle_text_h1_header() -> None:
    """The first line of bundle_text is the H1 header."""
    bundle = _assemble_default()
    first_line = bundle.bundle_text.splitlines()[0]
    assert first_line == "# SECTOR RESEARCHER INPUT BUNDLE"


# ---------------------------------------------------------------------------
# Test 4: distillation_text passed through verbatim
# ---------------------------------------------------------------------------


def test_distillation_text_verbatim() -> None:
    """The distillation_text appears verbatim under the ## DISTILLATION OUTPUT section."""
    special_text = "=== SPECIAL MARKER XYZ ===\nsome unique content"
    bundle = _assemble_default(distillation_text=special_text)
    assert special_text in bundle.bundle_text


def test_distillation_text_stored_verbatim_in_field() -> None:
    """The distillation_text field stores the passed-in text exactly."""
    bundle = _assemble_default(distillation_text=_DISTILLATION_TEXT)
    assert bundle.distillation_text == _DISTILLATION_TEXT


def test_distillation_section_header_present() -> None:
    """The bundle_text contains the ## DISTILLATION OUTPUT header."""
    bundle = _assemble_default()
    assert "## DISTILLATION OUTPUT" in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 5: No separate regime section rendered
# ---------------------------------------------------------------------------


def test_no_separate_regime_section_when_not_in_distillation() -> None:
    """Bundle does not add a VOLATILITY REGIME section if distillation_text lacks it."""
    distillation_without_regime = "=== SECTOR TECH SEMIS ===\nNo regime info here."
    bundle = _assemble_default(distillation_text=distillation_without_regime)
    assert "VOLATILITY REGIME" not in bundle.bundle_text


def test_volatility_regime_only_when_distillation_contains_it() -> None:
    """VOLATILITY REGIME appears only when distillation_text already contains it."""
    distillation_with_regime = "=== VOLATILITY REGIME: BULL_TRENDING ===\nMore content."
    bundle_with = _assemble_default(distillation_text=distillation_with_regime)
    bundle_without = _assemble_default(distillation_text="No regime info here.")
    assert "VOLATILITY REGIME" in bundle_with.bundle_text
    assert "VOLATILITY REGIME" not in bundle_without.bundle_text


# ---------------------------------------------------------------------------
# Test 6: Empty headlines placeholder
# ---------------------------------------------------------------------------


def test_empty_headlines_render_placeholder() -> None:
    """When headlines is empty tuple, the placeholder line appears."""
    bundle = _assemble_default(qualitative_input=_make_qualitative(headlines=()))
    assert "(no qualifying headlines in window)" in bundle.bundle_text


def test_non_empty_headlines_no_placeholder() -> None:
    """When headlines exist, the placeholder line does NOT appear."""
    qualitative = _make_qualitative(headlines=(_make_headline(),))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "(no qualifying headlines in window)" not in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 7: Empty events placeholder
# ---------------------------------------------------------------------------


def test_empty_events_render_placeholder() -> None:
    """When events is empty tuple, the placeholder line appears."""
    bundle = _assemble_default(qualitative_input=_make_qualitative(events=()))
    assert "(no scheduled events in 72h window)" in bundle.bundle_text


def test_non_empty_events_no_placeholder() -> None:
    """When events exist, the placeholder line does NOT appear."""
    qualitative = _make_qualitative(events=(_make_event(),))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "(no scheduled events in 72h window)" not in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 8: Timestamps are ISO 8601 UTC
# ---------------------------------------------------------------------------


def test_as_of_timestamp_iso8601_utc() -> None:
    """The 'As of:' line in bundle_text contains an ISO 8601 UTC timestamp."""
    bundle = _assemble_default()
    lines = bundle.bundle_text.splitlines()
    as_of_line = next(line for line in lines if line.startswith("As of:"))
    # Should end with Z (UTC) and be parseable
    timestamp_str = as_of_line.split("As of:")[1].strip()
    assert timestamp_str.endswith("Z")
    assert datetime.fromisoformat(timestamp_str).tzinfo is not None


def test_headline_published_at_iso8601_utc() -> None:
    """Headline published_at in bundle_text is ISO 8601 UTC."""
    pub_at = datetime(2024, 3, 14, 9, 0, 0, tzinfo=UTC)
    headline = _make_headline(published_at=pub_at)
    qualitative = _make_qualitative(headlines=(headline,))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "2024-03-14T09:00:00Z" in bundle.bundle_text


def test_event_time_iso8601_utc() -> None:
    """Event event_time in bundle_text is ISO 8601 UTC."""
    ev_time = datetime(2024, 3, 16, 16, 0, 0, tzinfo=UTC)
    event = _make_event(event_time=ev_time)
    qualitative = _make_qualitative(events=(event,))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "2024-03-16T16:00:00Z" in bundle.bundle_text


def test_data_freshness_iso8601_utc() -> None:
    """Data freshness in bundle_text is ISO 8601 UTC."""
    bundle = _assemble_default()
    assert "2024-03-15T14:00:00Z" in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 9: Determinism (byte-identical on repeated calls)
# ---------------------------------------------------------------------------


def test_deterministic_on_repeated_calls() -> None:
    """Calling assemble_input_bundle twice with identical inputs produces identical bundle_text."""
    qualitative = _make_qualitative(
        headlines=(_make_headline(),),
        events=(_make_event(),),
    )
    bundle1 = _assemble_default(qualitative_input=qualitative)
    bundle2 = _assemble_default(qualitative_input=qualitative)
    assert bundle1.bundle_text == bundle2.bundle_text


# ---------------------------------------------------------------------------
# Test 10: Full bundle structure — section order
# ---------------------------------------------------------------------------


def test_section_order() -> None:
    """DISTILLATION OUTPUT section comes before SECTOR-SPECIFIC QUALITATIVE INPUT."""
    bundle = _assemble_default()
    dist_pos = bundle.bundle_text.index("## DISTILLATION OUTPUT")
    qual_pos = bundle.bundle_text.index("## SECTOR-SPECIFIC QUALITATIVE INPUT")
    assert dist_pos < qual_pos


def test_bundle_contains_invocation_id() -> None:
    """The Invocation line appears in bundle_text."""
    bundle = _assemble_default()
    assert f"Invocation: {_INVOCATION_ID}" in bundle.bundle_text


def test_bundle_contains_sector_value() -> None:
    """The Sector line contains the sector enum value."""
    bundle = _assemble_default()
    assert f"Sector: {Sector.TECH_SEMIS.value}" in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 11: No RegimeLabel or RegimeContext Pydantic class defined in module
# ---------------------------------------------------------------------------


def test_no_regime_pydantic_classes_in_module() -> None:
    """The input_bundle module does not define RegimeLabel or RegimeContext Pydantic classes."""
    import alphamind.analysis.domain_researchers.input_bundle as module

    for name, obj in inspect.getmembers(module, inspect.isclass):
        if (
            name in ("RegimeLabel", "RegimeContext")
            and issubclass(obj, BaseModel)
            and obj.__module__ == module.__name__
        ):
            pytest.fail(f"{name} is a Pydantic model defined in input_bundle module")


# ---------------------------------------------------------------------------
# Test 12: Headline rendering with content
# ---------------------------------------------------------------------------


def test_headline_rendered_with_index_outlet_tickers_tags() -> None:
    """Non-empty headline is rendered with [1] prefix, outlet, tickers, tags."""
    headline = _make_headline(
        headline="NVDA beats Q1 estimates",
        source_outlet="Reuters",
        tier="tier_1",
        tickers=("NVDA", "AMD"),
        tags=(HeadlineType.EARNINGS_RELATED,),
    )
    qualitative = _make_qualitative(headlines=(headline,))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "[1]" in bundle.bundle_text
    assert "NVDA beats Q1 estimates" in bundle.bundle_text
    assert "Reuters" in bundle.bundle_text
    assert "NVDA, AMD" in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 13: Event rendering with content
# ---------------------------------------------------------------------------


def test_event_rendered_with_name_sectors_tickers_consensus() -> None:
    """Non-empty event is rendered with name, sectors, tickers, consensus."""
    event = _make_event(
        event_name="NVDA Q1 Earnings",
        sectors=frozenset({Sector.TECH_SEMIS}),
        tickers=("NVDA",),
        consensus="EPS $5.16, revenue $22.1B",
    )
    qualitative = _make_qualitative(events=(event,))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "NVDA Q1 Earnings" in bundle.bundle_text
    assert "tech_semis" in bundle.bundle_text
    assert "NVDA" in bundle.bundle_text
    assert "EPS $5.16" in bundle.bundle_text


def test_event_macro_tickers_when_no_tickers() -> None:
    """Event with no tickers renders '(macro)'."""
    event = _make_event(tickers=())
    qualitative = _make_qualitative(events=(event,))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "(macro)" in bundle.bundle_text


def test_event_no_consensus_renders_none_label() -> None:
    """Event with no consensus renders '(none)'."""
    event = _make_event(consensus=None)
    qualitative = _make_qualitative(events=(event,))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "(none)" in bundle.bundle_text


def test_cross_sector_event_renders_cross_sector() -> None:
    """Event with empty sectors frozenset renders '(cross-sector)'."""
    event = _make_event(sectors=frozenset())
    qualitative = _make_qualitative(events=(event,))
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "(cross-sector)" in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 14: Lookback window hours rendered
# ---------------------------------------------------------------------------


def test_lookback_window_hours_in_qualitative_section() -> None:
    """The lookback window hours appear in the qualitative section."""
    qualitative = _make_qualitative(lookback_window_hours=48)
    bundle = _assemble_default(qualitative_input=qualitative)
    assert "last 48 hours" in bundle.bundle_text


# ---------------------------------------------------------------------------
# Test 15: Snapshot — full bundle text for non-empty case
# ---------------------------------------------------------------------------


_DIST_HEADER = (
    "## DISTILLATION OUTPUT (sector slice"
    " — includes regime, anomaly summary, universal context, sector indicators)"
)
_EVENT_LINE = (
    "- 2024-03-15T14:30:00Z | NVDA Earnings Call"
    " | sectors: tech_semis | tickers: NVDA | consensus: EPS $5.16, revenue $22.1B"
)
_HEADLINE_LINE = (
    "[1] 2024-03-15T14:30:00Z [tier_1] Bloomberg | NVDA | earnings_related | NVDA beats estimates"
)

SNAPSHOT_BUNDLE_TEXT = "\n".join(
    [
        "# SECTOR RESEARCHER INPUT BUNDLE",
        "Invocation: inv-test-001",
        "Sector: tech_semis",
        "As of: 2024-03-15T14:30:00Z",
        "",
        _DIST_HEADER,
        "",
        "=== SECTOR TECH SEMIS ===",
        "Some distillation content here.",
        "=== UNIVERSAL CONTEXT ===",
        "Market regime: BULL_TRENDING",
        "",
        "## SECTOR-SPECIFIC QUALITATIVE INPUT",
        "Lookback window: last 24 hours",
        "Data freshness: 2024-03-15T14:00:00Z",
        "",
        "### HEADLINES (top 1 by composite score)",
        _HEADLINE_LINE,
        "",
        "### SCHEDULED EVENTS (next 72h)",
        _EVENT_LINE,
        "",
    ]
)


def test_snapshot_full_bundle_text() -> None:
    """Full bundle text matches canonical snapshot for non-empty case."""
    headline = _make_headline(
        headline="NVDA beats estimates",
        source_outlet="Bloomberg",
        tier="tier_1",
        published_at=_AS_OF,
        tickers=("NVDA",),
        tags=(HeadlineType.EARNINGS_RELATED,),
    )
    event = _make_event(
        event_id="evt-001",
        event_name="NVDA Earnings Call",
        event_time=_AS_OF,
        sectors=frozenset({Sector.TECH_SEMIS}),
        tickers=("NVDA",),
        consensus="EPS $5.16, revenue $22.1B",
    )
    qualitative = _make_qualitative(
        headlines=(headline,),
        events=(event,),
    )
    bundle = assemble_input_bundle(
        sector=Sector.TECH_SEMIS,
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        distillation_text=_DISTILLATION_TEXT,
        qualitative_input=qualitative,
    )
    assert bundle.bundle_text == SNAPSHOT_BUNDLE_TEXT
