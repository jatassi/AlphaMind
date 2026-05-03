"""Tests for the synthesizer input-bundle assembler — ALP-207."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from alphamind.analysis.synthesizer.input_bundle import assemble_input_bundle
from alphamind.analysis.synthesizer.models import BriefBundle, BriefSource

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)


def _bundle(source: BriefSource, text: str = "body", minutes_ago: int = 5) -> BriefBundle:
    return BriefBundle(
        source=source,
        text=text,
        freshness=_NOW - timedelta(minutes=minutes_ago),
    )


_PORTFOLIO_TOOLS: tuple[str, ...] = (
    "get_positions_summary",
    "get_active_theses_summary",
    "get_exposure_snapshot",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_six_bundle_assembly() -> None:
    """Six bundles in arbitrary input order render in canonical CR → SA-TECH →
    SA-FIN → SA-ENERGY → QR → AR order."""
    # Supply in deliberately scrambled order.
    bundles = (
        _bundle(BriefSource.AR, text="ar body"),
        _bundle(BriefSource.SA_FIN, text="fin body"),
        _bundle(BriefSource.QR, text="qr body"),
        _bundle(BriefSource.CR, text="cr body"),
        _bundle(BriefSource.SA_ENERGY, text="energy body"),
        _bundle(BriefSource.SA_TECH, text="tech body"),
    )
    out = assemble_input_bundle(
        regime_label="vol_expansion",
        brief_bundles=bundles,
        portfolio_tool_names=_PORTFOLIO_TOOLS,
        now_utc=_NOW,
    )

    # Each canonical label appears exactly once.
    cr_idx = out.index("Correlation/regime brief")
    tech_idx = out.index("Tech & semis sector brief")
    fin_idx = out.index("Financials sector brief")
    energy_idx = out.index("Energy sector brief")
    qr_idx = out.index("Baseline qualitative brief")
    ar_idx = out.index("Adaptive research findings")

    assert cr_idx < tech_idx < fin_idx < energy_idx < qr_idx < ar_idx

    # Each bundle's text is included.
    assert "cr body" in out
    assert "tech body" in out
    assert "fin body" in out
    assert "energy body" in out
    assert "qr body" in out
    assert "ar body" in out


def test_partial_bundles() -> None:
    """Only three bundles supplied → only those three sections render."""
    bundles = (
        _bundle(BriefSource.CR, text="cr body"),
        _bundle(BriefSource.SA_TECH, text="tech body"),
        _bundle(BriefSource.QR, text="qr body"),
    )
    out = assemble_input_bundle(
        regime_label="vol_expansion",
        brief_bundles=bundles,
        portfolio_tool_names=_PORTFOLIO_TOOLS,
        now_utc=_NOW,
    )
    assert "Correlation/regime brief" in out
    assert "Tech & semis sector brief" in out
    assert "Baseline qualitative brief" in out
    # Sources not supplied do not render.
    assert "Financials sector brief" not in out
    assert "Energy sector brief" not in out
    assert "Adaptive research findings" not in out


def test_regime_label_appears_first() -> None:
    """The volatility regime label is the first rendered line."""
    out = assemble_input_bundle(
        regime_label="vol_expansion",
        brief_bundles=(_bundle(BriefSource.CR),),
        portfolio_tool_names=_PORTFOLIO_TOOLS,
        now_utc=_NOW,
    )
    first_line = out.splitlines()[0]
    assert first_line == "Volatility regime: vol_expansion"
    # Tool reminder lands after regime, before any brief section.
    regime_idx = out.index("Volatility regime:")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    cr_idx = out.index("Correlation/regime brief")
    assert regime_idx < tools_idx < cr_idx


def test_portfolio_tool_names_listed() -> None:
    """All supplied portfolio-tool names appear under the AVAILABLE TOOLS section."""
    out = assemble_input_bundle(
        regime_label="low_vol_compression",
        brief_bundles=(_bundle(BriefSource.CR),),
        portfolio_tool_names=_PORTFOLIO_TOOLS,
        now_utc=_NOW,
    )
    for name in _PORTFOLIO_TOOLS:
        assert name in out


def test_freshness_minutes_ago() -> None:
    """Known timestamp delta produces 'Nm ago' string."""
    bundles = (
        BriefBundle(
            source=BriefSource.CR,
            text="cr body",
            freshness=_NOW - timedelta(minutes=17),
        ),
    )
    out = assemble_input_bundle(
        regime_label="vol_expansion",
        brief_bundles=bundles,
        portfolio_tool_names=_PORTFOLIO_TOOLS,
        now_utc=_NOW,
    )
    assert "freshness: 17m ago" in out


def test_freshness_negative_clamped() -> None:
    """freshness > now_utc renders '0m ago' rather than negative minutes."""
    bundles = (
        BriefBundle(
            source=BriefSource.CR,
            text="cr body",
            freshness=_NOW + timedelta(minutes=5),
        ),
    )
    out = assemble_input_bundle(
        regime_label="vol_expansion",
        brief_bundles=bundles,
        portfolio_tool_names=_PORTFOLIO_TOOLS,
        now_utc=_NOW,
    )
    assert "freshness: 0m ago" in out
    assert "-5m" not in out


def test_source_labels_match_prompt() -> None:
    """The labels emitted by the assembler appear verbatim in the system
    prompt's ``<inputs>`` block."""
    repo_root = Path(__file__).resolve().parents[3]
    prompt_text = (repo_root / "prompts" / "analysis" / "synthesizer.md").read_text(
        encoding="utf-8"
    )

    # Render an assembled bundle covering every source so all labels surface.
    bundles = tuple(_bundle(source) for source in BriefSource)
    out = assemble_input_bundle(
        regime_label="vol_expansion",
        brief_bundles=bundles,
        portfolio_tool_names=_PORTFOLIO_TOOLS,
        now_utc=_NOW,
    )

    expected_labels = (
        "Correlation/regime brief",
        "Tech & semis sector brief",
        "Financials sector brief",
        "Energy sector brief",
        "Baseline qualitative brief",
        "Adaptive research findings",
    )
    for label in expected_labels:
        assert label in out, f"label {label!r} not rendered by assembler"
        assert label in prompt_text, f"label {label!r} missing from prompt <inputs> block"
