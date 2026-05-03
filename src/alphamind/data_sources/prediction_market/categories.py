"""
Canonical AlphaMind category derivation for prediction-market contracts.

Used by the Polymarket and Kalshi vendor adapters to assign every
``prediction_market_contracts.category`` row to one of 11 canonical
categories or the sentinel ``other``.

Vendor labels — Polymarket ``/events`` ``tags[].label`` or Kalshi
``event.category`` — act as a pre-filter.  Events without any passthrough
label become ``other`` immediately and skip keyword matching.  This both
collapses obviously off-topic contracts (sports, crypto, entertainment)
to a single bucket and keeps the description-keyword scan cheap on the
ones that survive.
"""

from __future__ import annotations

import re

CANONICAL_CATEGORIES: tuple[str, ...] = (
    "monetary_policy",
    "antitrust",
    "trade",
    "financial_regulation",
    "fiscal_policy",
    "election",
    "opec",
    "conflict",
    "sanctions",
    "china_policy",
    "corporate_action",
)

OTHER: str = "other"

#: Union of accepted Polymarket ``/events`` ``tags[].label`` values and
#: Kalshi ``event.category`` values.  Compared case-insensitively.
_PASSTHROUGH_LABELS: frozenset[str] = frozenset(
    label.lower()
    for label in (
        # Polymarket
        "Politics",
        "Elections",
        "US Election",
        "Midterms",
        "Senate midterms",
        "Governor midterms",
        "Global Elections",
        "World",
        "Geopolitics",
        "Ukraine",
        "Trump",
        "Trump Presidency",
        "Finance",
        "Tech",
        # Kalshi
        "Economics",
        "Financials",
        "Companies",
    )
)


def _rule(category: str, *alternatives: str) -> tuple[str, re.Pattern[str]]:
    pattern = re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", re.IGNORECASE)
    return category, pattern


# First match wins.  Ordering is specificity-first — single-keyword targets
# (opec, china_policy) precede broader topics (conflict, election) so a
# Taiwan-strait market lands in china_policy rather than conflict.
_KEYWORD_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    _rule(
        "monetary_policy",
        "fed",
        "fomc",
        "federal reserve",
        "rate cut",
        "rate hike",
        "interest rate",
        "interest rates",
        "powell",
    ),
    _rule("opec", "opec", "crude oil", "petroleum", "oil production"),
    _rule("sanctions", "sanction", "sanctions", "embargo", "export ban"),
    _rule(
        "china_policy",
        "china",
        "chinese",
        "beijing",
        "xi jinping",
        "hong kong",
        "taiwan",
    ),
    _rule(
        "conflict",
        "war",
        "invasion",
        "military",
        "nato",
        "ukraine",
        "russia",
        "russian",
        "gaza",
        "israel",
        "iran",
        "houthi",
    ),
    _rule("antitrust", "antitrust", "ftc", "doj", "monopoly", "anti-competitive"),
    _rule(
        "trade",
        "tariff",
        "tariffs",
        "trade war",
        "trade deal",
        "import duty",
        "export control",
    ),
    _rule(
        "financial_regulation",
        "sec",
        "cfpb",
        "dodd-frank",
        "banking regulation",
        "financial regulation",
        "basel",
        "finra",
    ),
    _rule(
        "fiscal_policy",
        "tax cut",
        "tax cuts",
        "tax reform",
        "corporate tax",
        "irs",
        "budget",
        "government shutdown",
        "shutdown",
        "debt ceiling",
        "deficit",
    ),
    _rule(
        "election",
        "election",
        "elections",
        "president",
        "presidential",
        "congress",
        "senate",
        "ballot",
        "vote",
        "votes",
        "democrat",
        "democrats",
        "republican",
        "republicans",
        "primary",
        "midterm",
        "midterms",
        "governor",
        "mayor",
    ),
    _rule(
        "corporate_action",
        "merger",
        "acquisition",
        "ipo",
        "spin-off",
        "buyback",
        "bankruptcy",
        "layoff",
        "layoffs",
    ),
)


def derive_canonical_category(*, description: str, vendor_labels: tuple[str, ...]) -> str:
    """Return the canonical AlphaMind category for the given contract.

    Pre-filter: when no ``vendor_labels`` entry matches a passthrough label,
    return ``other`` without scanning the description.  Otherwise the first
    keyword pattern that fires against the lowercased description wins.
    Falls through to ``other`` when the description matches no rule.
    """
    if not any(label.lower() in _PASSTHROUGH_LABELS for label in vendor_labels):
        return OTHER

    for category, pattern in _KEYWORD_RULES:
        if pattern.search(description):
            return category
    return OTHER
