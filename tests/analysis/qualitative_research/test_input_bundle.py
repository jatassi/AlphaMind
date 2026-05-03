"""Tests for qualitative-researcher input bundle assembler — ALP-251."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.analysis._shared import Sector
from alphamind.analysis.qualitative_research.loaders import (
    ActiveThesis,
    CalendarEvent,
    PredictionMarketSnapshot,
    QualitativeInputs,
    SentimentAggregate,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_AS_OF = datetime(2024, 3, 15, 14, 30, 0, tzinfo=UTC)
_INVOCATION_ID = "inv-qr-001"

_REGIME_LABEL: dict[str, object] = {
    "regime_label": "vol_expansion",
    "transition_state": "early-weak",
    "prior_label": "low_vol_compression",
    "invocations_held": 3,
    "indicator_agreement_count": 4,
    "regime_skip_emergency": False,
    "vix_level": 18.5,
    "term_structure_basis": 1.2,
    "vvix_percentile": 0.65,
    "realized_vol_5d": 0.18,
    "realized_vol_20d": 0.16,
}

_DIGEST = NewsDigest(
    as_of=_AS_OF,
    last_invocation_time=datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC),
    total_collected=20,
    total_shown=10,
    entries=(),
    digest_text="[ND-M1] 2024-03-15T10:00:00Z [tier_1] Fed holds rates\n      Outlet: Reuters",
)


def _make_sentiment(
    ticker: str = "AAPL",
    directional_score: float = 0.5,
    magnitude: float = 0.6,
    rate_of_change: float | None = 0.1,
    volume: int | None = 100,
    divergence_flag: bool | None = False,
    percentile_vs_self: float = 0.75,
) -> SentimentAggregate:
    return SentimentAggregate(
        ticker=ticker,
        directional_score=directional_score,
        magnitude=magnitude,
        rate_of_change=rate_of_change,
        volume=volume,
        divergence_flag=divergence_flag,
        percentile_vs_self=percentile_vs_self,
        data_freshness=_AS_OF,
    )


def _make_prediction_market(
    contract_id: str = "PM-001",
    description: str = "Fed holds rates",
    platform: str = "Polymarket",
    category: str = "macro",
    current_probability: float = 0.72,
    delta_since_last_invocation_pp: float = 3.0,
    delta_since_prior_pp: float = 1.5,
    volume_24h_usd: float | None = 50_000.0,
    expiration: str | None = "2024-03-20",
    is_low_liquidity: bool = False,
    meets_threshold_flag: bool = False,
) -> PredictionMarketSnapshot:
    return PredictionMarketSnapshot(
        contract_id=contract_id,
        description=description,
        platform=platform,
        category=category,
        current_probability=current_probability,
        delta_since_last_invocation_pp=delta_since_last_invocation_pp,
        delta_since_prior_pp=delta_since_prior_pp,
        volume_24h_usd=volume_24h_usd,
        expiration=expiration,
        is_low_liquidity=is_low_liquidity,
        meets_threshold_flag=meets_threshold_flag,
        data_freshness=_AS_OF,
    )


def _make_calendar_event(
    event_id: str = "EVT-001",
    event_name: str = "FOMC Meeting",
    event_time: datetime | None = None,
    event_type: str = "macro",
    tickers: tuple[str, ...] = (),
    sectors: frozenset[Sector] | None = None,
    consensus: str | None = None,
) -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        event_name=event_name,
        event_time=event_time or _AS_OF,
        event_type=event_type,
        tickers=tickers,
        sectors=sectors if sectors is not None else frozenset(),
        consensus=consensus,
    )


def _make_thesis(
    thesis_id: str = "TH-001",
    ticker: str = "NVDA",
    summary: str = "AI capex supercycle drives outperformance",
    key_catalyst: str = "Q1 earnings beat",
    time_expectation_hours: int = 48,
) -> ActiveThesis:
    return ActiveThesis(
        thesis_id=thesis_id,
        ticker=ticker,
        summary=summary,
        key_catalyst=key_catalyst,
        time_expectation_hours=time_expectation_hours,
    )


def _make_inputs(
    sentiment: tuple[SentimentAggregate, ...] = (),
    prediction_markets: tuple[PredictionMarketSnapshot, ...] = (),
    events: tuple[CalendarEvent, ...] = (),
    theses: tuple[ActiveThesis, ...] = (),
) -> QualitativeInputs:
    return QualitativeInputs(
        sentiment_aggregates=sentiment,
        prediction_markets=prediction_markets,
        events=events,
        theses=theses,
        data_freshness=_AS_OF,
    )


# ---------------------------------------------------------------------------
# Import resolves, assemble_input_bundle returns InputBundle
# ---------------------------------------------------------------------------


def test_import_resolves() -> None:
    """assemble_input_bundle and InputBundle are importable from the module."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        InputBundle,
        assemble_input_bundle,
    )

    assert callable(assemble_input_bundle)
    from pydantic import BaseModel

    assert issubclass(InputBundle, BaseModel)


# ---------------------------------------------------------------------------
# bundle_text contains all six section headers
# ---------------------------------------------------------------------------


def test_bundle_text_contains_all_section_headers() -> None:
    """bundle_text contains every required section header."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    headers = [
        "## VOLATILITY REGIME",
        "## NEWS DIGEST",
        "## SENTIMENT AGGREGATES",
        "## PREDICTION-MARKET SNAPSHOT",
        "## EVENT CALENDAR (next 72h)",
        "## ACTIVE THESIS SUMMARIES",
    ]
    for header in headers:
        assert header in bundle.bundle_text, f"Missing header: {header}"


# ---------------------------------------------------------------------------
# Empty theses → placeholder; non-empty → formatted rows
# ---------------------------------------------------------------------------


def test_empty_theses_placeholder() -> None:
    """Empty theses tuple produces the named placeholder line."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(theses=()),
    )
    placeholder = "(no active theses — execution-layer thesis model pending per ALP-111)."
    assert placeholder in bundle.bundle_text


def test_non_empty_theses_formatted_rows() -> None:
    """Non-empty theses produce formatted rows (no placeholder)."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    thesis = _make_thesis(thesis_id="TH-001", ticker="NVDA", summary="AI capex supercycle")
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(theses=(thesis,)),
    )
    assert "[TH-001] NVDA: AI capex supercycle" in bundle.bundle_text
    assert "(no active theses" not in bundle.bundle_text


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def test_sentiment_sorted_alphabetically() -> None:
    """Sentiment rows appear in alphabetical ticker order."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    s_z = _make_sentiment(ticker="ZZZZ")
    s_a = _make_sentiment(ticker="AAAA")
    s_m = _make_sentiment(ticker="MMMM")
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(sentiment=(s_z, s_a, s_m)),
    )
    pos_a = bundle.bundle_text.index("AAAA:")
    pos_m = bundle.bundle_text.index("MMMM:")
    pos_z = bundle.bundle_text.index("ZZZZ:")
    assert pos_a < pos_m < pos_z


def test_calendar_events_sorted_by_event_time() -> None:
    """Calendar event rows appear sorted by event_time ascending."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    e_late = _make_calendar_event(
        event_id="EVT-C",
        event_name="Late Event",
        event_time=datetime(2024, 3, 17, 12, 0, 0, tzinfo=UTC),
    )
    e_early = _make_calendar_event(
        event_id="EVT-A",
        event_name="Early Event",
        event_time=datetime(2024, 3, 15, 8, 0, 0, tzinfo=UTC),
    )
    e_mid = _make_calendar_event(
        event_id="EVT-B",
        event_name="Middle Event",
        event_time=datetime(2024, 3, 16, 0, 0, 0, tzinfo=UTC),
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(events=(e_late, e_early, e_mid)),
    )
    pos_early = bundle.bundle_text.index("Early Event")
    pos_mid = bundle.bundle_text.index("Middle Event")
    pos_late = bundle.bundle_text.index("Late Event")
    assert pos_early < pos_mid < pos_late


def test_prediction_markets_sorted_by_contract_id() -> None:
    """Prediction market rows appear sorted by contract_id."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    pm_z = _make_prediction_market(contract_id="PM-ZZZ")
    pm_a = _make_prediction_market(contract_id="PM-AAA")
    pm_m = _make_prediction_market(contract_id="PM-MMM")
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(prediction_markets=(pm_z, pm_a, pm_m)),
    )
    pos_a = bundle.bundle_text.index("PM-AAA")
    pos_m = bundle.bundle_text.index("PM-MMM")
    pos_z = bundle.bundle_text.index("PM-ZZZ")
    assert pos_a < pos_m < pos_z


# ---------------------------------------------------------------------------
# Determinism — byte-identical on repeated calls
# ---------------------------------------------------------------------------


def test_determinism_identical_inputs() -> None:
    """Calling assemble_input_bundle twice with identical inputs → byte-identical bundle_text."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    inputs = _make_inputs(
        sentiment=(
            _make_sentiment(ticker="TSLA"),
            _make_sentiment(ticker="AAPL"),
        ),
        prediction_markets=(_make_prediction_market(contract_id="PM-001"),),
        events=(_make_calendar_event(event_id="EVT-001"),),
        theses=(_make_thesis(thesis_id="TH-001"),),
    )

    bundle1 = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=inputs,
    )
    bundle2 = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=inputs,
    )
    assert bundle1.bundle_text == bundle2.bundle_text


# ---------------------------------------------------------------------------
# digest_text embedded verbatim
# ---------------------------------------------------------------------------


def test_digest_text_embedded_verbatim() -> None:
    """digest.digest_text appears verbatim in bundle_text — not re-decorated."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    unique_marker = "UNIQUE_DIGEST_MARKER_XYZ_12345"
    custom_digest = NewsDigest(
        as_of=_AS_OF,
        last_invocation_time=datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC),
        total_collected=1,
        total_shown=1,
        entries=(),
        digest_text=unique_marker,
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=custom_digest,
        inputs=_make_inputs(),
    )
    assert unique_marker in bundle.bundle_text
    assert bundle.digest_text == unique_marker


# ---------------------------------------------------------------------------
# Regime fields appear in regime_text and bundle_text
# ---------------------------------------------------------------------------


def test_regime_fields_in_regime_text() -> None:
    """All four regime fields appear in InputBundle.regime_text."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    assert "vol_expansion" in bundle.regime_text
    assert "early-weak" in bundle.regime_text
    assert "low_vol_compression" in bundle.regime_text
    assert "3" in bundle.regime_text  # invocations_held


def test_regime_text_in_bundle_text() -> None:
    """The regime block appears under ## VOLATILITY REGIME in bundle_text."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    regime_pos = bundle.bundle_text.index("## VOLATILITY REGIME")
    digest_pos = bundle.bundle_text.index("## NEWS DIGEST")
    # Regime block comes before NEWS DIGEST
    assert regime_pos < digest_pos
    # All four labels are visible in bundle_text
    assert "vol_expansion" in bundle.bundle_text
    assert "early-weak" in bundle.bundle_text
    assert "low_vol_compression" in bundle.bundle_text


def test_regime_text_renders_every_key_alphabetically() -> None:
    """Every key in regime_label is rendered, in deterministic alphabetical
    order, as ``key: value`` lines.

    The full payload from ``assemble_regime_block`` carries seven supporting
    indicators in addition to the four labels — the LLM should see every key
    so it can read the indicator snapshot.
    """
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    expected_keys = sorted(_REGIME_LABEL.keys())
    expected_lines = [f"{k}: {_REGIME_LABEL[k]}" for k in expected_keys]
    rendered = bundle.regime_text
    # Each rendered line is a substring of regime_text.
    for line in expected_lines:
        assert line in rendered, f"missing rendered line: {line!r}"
    # Lines appear in alphabetical key order.
    positions = [rendered.index(line) for line in expected_lines]
    assert positions == sorted(positions), "keys not in alphabetical order"


def test_regime_text_handles_arbitrary_extra_keys() -> None:
    """Renderer is payload-agnostic: any extra keys present render too.

    This protects against the regime payload growing without the bundle
    assembler noticing.
    """
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    custom = dict(_REGIME_LABEL)
    custom["new_indicator"] = 42
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=custom,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    assert "new_indicator: 42" in bundle.regime_text


def test_regime_text_renders_none_prior_label_as_string() -> None:
    """A None ``prior_label`` (bootstrap state) renders as the literal None
    so the LLM sees the absence rather than a missing field.
    """
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    custom = dict(_REGIME_LABEL)
    custom["prior_label"] = None
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=custom,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    assert "prior_label: None" in bundle.regime_text


# ---------------------------------------------------------------------------
# Prediction-market flags, frozen model, header line
# ---------------------------------------------------------------------------


def test_prediction_market_flagged_appended() -> None:
    """[FLAGGED] is appended when meets_threshold_flag=True."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    pm = _make_prediction_market(contract_id="PM-F01", meets_threshold_flag=True)
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(prediction_markets=(pm,)),
    )
    assert "[FLAGGED]" in bundle.bundle_text


def test_prediction_market_low_liquidity_appended() -> None:
    """[LOW LIQUIDITY] is appended when is_low_liquidity=True."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    pm = _make_prediction_market(contract_id="PM-L01", is_low_liquidity=True)
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(prediction_markets=(pm,)),
    )
    assert "[LOW LIQUIDITY]" in bundle.bundle_text


def test_sentiment_renders_pending_for_none_stub_fields() -> None:
    """When the v1 stub fields are None, the renderer prints 'pending' so the
    LLM reads them as 'data not available yet' rather than 'no signal'.
    """
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    s = _make_sentiment(
        ticker="NVDA",
        rate_of_change=None,
        volume=None,
        divergence_flag=None,
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(sentiment=(s,)),
    )
    assert "change=pending" in bundle.bundle_text
    assert "vol=pending" in bundle.bundle_text
    assert "divergence=pending" in bundle.bundle_text


def test_sentiment_renders_concrete_values_when_present() -> None:
    """When the v1 stub fields are populated (future), the renderer prints them."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    s = _make_sentiment(
        ticker="NVDA",
        rate_of_change=0.25,
        volume=42,
        divergence_flag=True,
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(sentiment=(s,)),
    )
    assert "change=0.25" in bundle.bundle_text
    assert "vol=42" in bundle.bundle_text
    assert "divergence=True" in bundle.bundle_text


def test_prediction_market_renders_delta_since_prior_label() -> None:
    """Prediction-market row uses the accurate Δ_since_prior label, not the
    misleading Δ_24h label.

    The underlying field measures the change versus the second-latest history
    row, which can be any timestamp ago — not necessarily 24h.
    """
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    pm = _make_prediction_market(
        contract_id="PM-D01",
        delta_since_prior_pp=4.2,
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(prediction_markets=(pm,)),
    )
    assert "Δ_since_prior=4.2pp" in bundle.bundle_text
    assert "Δ_24h" not in bundle.bundle_text


def test_input_bundle_is_frozen() -> None:
    """InputBundle is a frozen Pydantic model."""
    from pydantic import BaseModel, ValidationError

    from alphamind.analysis.qualitative_research.input_bundle import (
        InputBundle,
        assemble_input_bundle,
    )

    assert issubclass(InputBundle, BaseModel)
    assert InputBundle.model_config.get("frozen") is True

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    with pytest.raises(ValidationError):
        bundle.invocation_id = "mutated"  # type: ignore[misc]


def test_bundle_header_and_invocation_line() -> None:
    """bundle_text starts with the H1 header and contains the invocation line."""
    from alphamind.analysis.qualitative_research.input_bundle import (
        assemble_input_bundle,
    )

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        digest=_DIGEST,
        inputs=_make_inputs(),
    )
    lines = bundle.bundle_text.splitlines()
    assert lines[0] == "# QUALITATIVE RESEARCHER INPUT BUNDLE"
    assert f"Invocation: {_INVOCATION_ID}" in bundle.bundle_text
    assert "As of: 2024-03-15T14:30:00Z" in bundle.bundle_text
