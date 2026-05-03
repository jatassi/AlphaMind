"""
Tests for ``src/alphamind/data_sources/prediction_market/categories.py``.

Covers each of the 11 canonical categories plus pre-filter rejection paths
(unknown vendor labels, empty vendor labels, ambiguous descriptions).
"""

from __future__ import annotations

import pytest

from alphamind.data_sources.prediction_market.categories import (
    CANONICAL_CATEGORIES,
    OTHER,
    derive_canonical_category,
)

_LABELS = ("Politics", "World", "Finance")  # mix Polymarket + Kalshi passthroughs


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Will the Fed cut rates in May?", "monetary_policy"),
        ("Will the FOMC pause this meeting?", "monetary_policy"),
        ("Will Powell signal a hike?", "monetary_policy"),
        ("Will the FTC block the Adobe-Figma merger?", "antitrust"),
        ("Will the DOJ open an antitrust probe?", "antitrust"),
        ("Will Trump impose new tariffs on imports?", "trade"),
        ("Will a new trade war start with China?", "china_policy"),
        ("Will the SEC approve the spot ETH ETF?", "financial_regulation"),
        ("Will Congress pass tax cuts this year?", "fiscal_policy"),
        ("Will there be a government shutdown?", "fiscal_policy"),
        ("Will the Republican candidate win the 2028 presidential election?", "election"),
        ("Will the Democrats hold the Senate in the midterms?", "election"),
        ("Will OPEC extend production cuts?", "opec"),
        ("Will Russia withdraw from Ukraine?", "conflict"),
        ("Will Israel and Iran sign a ceasefire?", "conflict"),
        ("Will the US lift sanctions on Iran?", "sanctions"),
        ("Will China invade Taiwan in 2026?", "china_policy"),
        ("Will Apple complete the buyback by year end?", "corporate_action"),
        ("Will OpenAI go through with an IPO?", "corporate_action"),
    ],
)
def test_canonical_category_for_passthrough_label(description: str, expected: str) -> None:
    assert derive_canonical_category(description=description, vendor_labels=_LABELS) == expected


def test_all_eleven_canonical_buckets_reachable() -> None:
    """Each of the 11 canonical buckets is producible by at least one description."""
    examples: dict[str, str] = {
        "monetary_policy": "Will the Fed cut rates?",
        "antitrust": "Will the FTC block the merger?",
        "trade": "Will tariffs be raised?",
        "financial_regulation": "Will the SEC approve the new rule?",
        "fiscal_policy": "Will the budget deal include tax cuts?",
        "election": "Will the Republican win the senate vote?",
        "opec": "Will OPEC extend cuts?",
        "conflict": "Will Israel and Iran reach a ceasefire?",
        "sanctions": "Will the US lift the embargo?",
        "china_policy": "Will Beijing tighten Taiwan policy?",
        "corporate_action": "Will the IPO price above range?",
    }
    assert set(examples.keys()) == set(CANONICAL_CATEGORIES)
    for canonical, desc in examples.items():
        assert derive_canonical_category(description=desc, vendor_labels=_LABELS) == canonical


# ---------------------------------------------------------------------------
# Pre-filter rejection
# ---------------------------------------------------------------------------


def test_off_topic_vendor_labels_short_circuit_to_other() -> None:
    """Sports/Crypto/Entertainment labels reject before keyword matching."""
    assert (
        derive_canonical_category(
            description="Will the Fed cut rates?",
            vendor_labels=("Sports", "Crypto", "Entertainment"),
        )
        == OTHER
    )


def test_empty_vendor_labels_yields_other() -> None:
    assert (
        derive_canonical_category(description="Will the Fed cut rates?", vendor_labels=()) == OTHER
    )


def test_passthrough_label_with_no_keyword_match_yields_other() -> None:
    """Politics-tagged event with completely off-topic question still becomes 'other'."""
    assert (
        derive_canonical_category(
            description="Will the next sci-fi movie cross $1B at the box office?",
            vendor_labels=("Politics", "World"),
        )
        == OTHER
    )


def test_label_matching_is_case_insensitive() -> None:
    assert (
        derive_canonical_category(
            description="Will the Fed cut rates?",
            vendor_labels=("politics",),
        )
        == "monetary_policy"
    )


def test_keyword_match_is_case_insensitive() -> None:
    assert (
        derive_canonical_category(
            description="WILL THE FED CUT RATES?",
            vendor_labels=("Politics",),
        )
        == "monetary_policy"
    )


# ---------------------------------------------------------------------------
# Specificity-first ordering — ambiguous descriptions
# ---------------------------------------------------------------------------


def test_china_keyword_beats_conflict() -> None:
    """A Taiwan-strait market lands in china_policy rather than conflict."""
    assert (
        derive_canonical_category(
            description="Will China invade Taiwan and trigger a wider war?",
            vendor_labels=("Geopolitics",),
        )
        == "china_policy"
    )


def test_opec_beats_conflict_when_both_apply() -> None:
    """A 'oil production' question with war-adjacent context lands in opec."""
    assert (
        derive_canonical_category(
            description="Will OPEC raise oil production despite the war in Yemen?",
            vendor_labels=("World",),
        )
        == "opec"
    )
