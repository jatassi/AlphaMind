"""Canonical news-tag taxonomy and vendor-tag normalization helpers.

Hoisted out of ``data_sources/_common.py`` (ALP-473) because the only
consumers are news-domain modules — ``finnhub/news.py``, ``sec_edgar/rss.py``,
``marketaux/news.py``, ``analysis/news_clustering/clustering.py``,
``analysis/qualitative_research/news_digest.py``,
``analysis/domain_researchers/qualitative_input.py``, and
``distillation/q7/correlation_regime_change.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

__all__ = ["HeadlineType", "decode_topic_tags", "normalize_vendor_tags"]


class HeadlineType(StrEnum):
    """Canonical headline-type taxonomy.

    Vendor tag vocabularies (Marketaux topics, Finnhub categories, SEC EDGAR
    8-K item codes, RSS topics) are normalized into this set at the collector
    boundary via ``config/headline_tag_mapping.yaml``. Multiple values per
    headline are allowed.

    The 14 members mirror ``docs/design/01-data-layer/schema/_common.py``
    § ``HeadlineType``.
    """

    BREAKING = "breaking"
    EARNINGS_RELATED = "earnings_related"
    M_AND_A = "m_and_a"
    ANALYST_ACTION = "analyst_action"
    REGULATORY = "regulatory"
    GEOPOLITICAL = "geopolitical"
    MACRO_DATA = "macro_data"
    INSIDER_ACTIVITY = "insider_activity"
    SHORT_REPORT = "short_report"
    ACTIVIST = "activist"
    PRODUCT_LAUNCH = "product_launch"
    SUPPLY_CHAIN = "supply_chain"
    GUIDANCE = "guidance"
    SECTOR_ROTATION = "sector_rotation"


@lru_cache(maxsize=1)
def _load_headline_tag_mapping() -> dict[str, dict[str, HeadlineType]]:
    """Read ``config/headline_tag_mapping.yaml`` once and cache the result.

    The yaml is keyed by vendor name (``marketaux``, ``finnhub``,
    ``sec_edgar_8k``, ``rss_topic``). Each vendor's mapping is a dict from
    raw vendor tag to canonical ``HeadlineType.value``. Unknown values in
    the yaml fail loudly at load time so a typo can't silently drop tags.
    """
    import yaml

    path = Path(__file__).parents[4] / "config" / "headline_tag_mapping.yaml"
    with path.open() as fh:
        raw: dict[str, dict[str, str]] = yaml.safe_load(fh) or {}

    mapping: dict[str, dict[str, HeadlineType]] = {}
    for vendor, vendor_tags in raw.items():
        mapping[vendor] = {
            raw_tag: HeadlineType(canonical) for raw_tag, canonical in vendor_tags.items()
        }
    return mapping


def normalize_vendor_tags(vendor: str, raw_tags: Iterable[str]) -> list[HeadlineType]:
    """Normalize vendor-raw tags into canonical ``HeadlineType`` values.

    Unmapped tags drop silently — the mapping yaml is the closed vocabulary,
    new vendor tags must be added to the yaml before they appear in
    persisted ``news_articles.topic_tags``. An unknown vendor key returns
    an empty list (no mapping table available).

    Order is preserved; duplicates in the input are preserved (callers
    deduplicate when they care).
    """
    table = _load_headline_tag_mapping().get(vendor)
    if table is None:
        return []
    return [table[t] for t in raw_tags if t in table]


def decode_topic_tags(raw: str | None) -> tuple[HeadlineType, ...]:
    """Decode a ``news_articles.topic_tags`` cell into typed ``HeadlineType`` values.

    ``topic_tags`` is canonically a JSON-serialized list per the storage
    spec; legacy rows may carry comma-separated values. Both shapes decode
    cleanly. Malformed JSON inside a ``[``-prefixed string returns ``()``
    rather than raising — historical rows can carry corrupt content.

    Unknown values drop silently — the canonical taxonomy in
    :class:`HeadlineType` is the single source of truth. Order is preserved.
    """
    if raw is None:
        return ()
    stripped = raw.strip()
    if not stripped:
        return ()
    if stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return ()
        candidates = [str(v) for v in decoded if isinstance(v, str)]
    else:
        candidates = [token.strip() for token in stripped.split(",") if token.strip()]
    out: list[HeadlineType] = []
    for tag in candidates:
        try:
            out.append(HeadlineType(tag))
        except ValueError:
            continue
    return tuple(out)
