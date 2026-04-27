"""
SEC EDGAR 8-K RSS collector — story 05h.

``collect_8k_filings(since, ...)`` pulls 8-K filings via the EDGAR browse-edgar
Atom feed, filters to universe tickers by CIK, and persists records to
``news_articles`` + ``news_article_tickers``.

Body retrieval: the primary HTML document is fetched, stripped to plain text,
and written to ``<body_dir>/<YYYY>/<MM>/<accession>.txt``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from alphamind.data_sources._common import default_session_factory, resume_since, track_run
from alphamind.data_sources.sec_edgar.client import SecEdgarClient
from alphamind.persistence.models import AssetUniverse, NewsArticles, NewsArticleTickers

logger = logging.getLogger(__name__)

_FULL_INDEX_RSS = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"
)
_EDGAR_NS = "https://www.sec.gov/Archives/edgar"
_ATOM_NS = "http://www.w3.org/2005/Atom"

# Match "Item X.YY" codes in filing descriptions.
_ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)", re.IGNORECASE)


def _default_user_agent() -> str:
    """Return the SEC User-Agent from env or a safe fallback."""
    return os.environ.get("SEC_EDGAR_USER_AGENT", "AlphaMind alphamind@example.com")


def collect_8k_filings(
    since: datetime | None = None,
    *,
    session_factory: Any = None,
    user_agent: str | None = None,
    _transport: httpx.BaseTransport | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _repo: Any = None,
    body_dir: Path | None = None,
) -> None:
    """
    Fetch 8-K filings from EDGAR since *since* and persist to ``news_articles``.

    Parameters
    ----------
    since:
        Only filings on or after this UTC date are written (YYYY-MM-DD prefix
        compared).  Defaults to the latest stored SEC 8-K article minus a
        2-hour overlap, or 24 hours ago when the table is empty.
    session_factory:
        SQLAlchemy session factory bound to the target database.  Defaults to
        ``default_session_factory()``.
    user_agent:
        ``User-Agent`` header value required by SEC (``AlphaMind <email>``).
        Defaults to ``$SEC_EDGAR_USER_AGENT`` env var or a safe placeholder.
    _transport:
        Injectable httpx transport for testing.
    _sleep:
        Injectable sleep for retry back-off.
    _repo:
        Injectable run-tracking repository (for testing).
    body_dir:
        Root directory for body text files.  Defaults to
        ``$USERPROFILE/AlphaMind/data/news``.
    """
    if session_factory is None:
        session_factory = default_session_factory()

    if since is None:
        since = resume_since(
            column=NewsArticles.published_at,
            filters=(NewsArticles.source == "sec_edgar_8k_rss",),
            default_lookback=timedelta(hours=24),
            overlap=timedelta(hours=2),
            session_factory=session_factory,
        )

    if user_agent is None:
        user_agent = _default_user_agent()

    client = SecEdgarClient(
        user_agent=user_agent,
        _transport=_transport,
        _sleep=_sleep,
    )

    if body_dir is None:
        userprofile = os.environ.get("USERPROFILE") or str(Path.home())
        body_dir = Path(userprofile) / "AlphaMind" / "data" / "news"

    since_date = since.strftime("%Y-%m-%d")

    with track_run("sec_edgar.8k_rss", _repo=_repo) as run:
        cik_to_ticker = _load_cik_map(session_factory)

        xml_text = client.get(_FULL_INDEX_RSS).text
        filings = _parse_rss(xml_text)

        rows_written = 0
        for filing in filings:
            # Skip filings before the requested window.
            if filing.get("date", "") < since_date:
                continue

            ticker = cik_to_ticker.get(filing.get("cik", "").lstrip("0"))
            if ticker is None:
                logger.warning(
                    "SEC EDGAR: filing CIK %s not in asset_universe; skipping",
                    filing.get("cik"),
                )
                continue

            accession = filing["accession"]
            if _article_exists(session_factory, accession):
                logger.debug("SEC EDGAR: accession %s already in DB; skipping", accession)
                continue

            body_path = _fetch_and_store_body(
                client, filing.get("link", ""), accession, filing["date"], body_dir
            )
            _write_article(session_factory, filing=filing, ticker=ticker, body_path=body_path)
            rows_written += 1

        run.rows_written = rows_written


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_cik_map(session_factory: Any) -> dict[str, str]:
    """Return mapping of stripped CIK (no leading zeros) → ticker."""
    with session_factory() as sess:
        rows = sess.execute(
            select(AssetUniverse.cik, AssetUniverse.ticker).where(AssetUniverse.cik.is_not(None))
        ).all()
    return {cik.lstrip("0"): ticker for cik, ticker in rows if cik}


def _parse_rss(xml_text: str) -> list[dict[str, str]]:
    """Parse Atom or RSS 2.0 XML and return 8-K filing metadata dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("SEC EDGAR: failed to parse RSS XML")
        return []

    # EDGAR browse-edgar returns Atom; fall back to RSS 2.0 for other sources.
    entries = root.findall(f".//{{{_ATOM_NS}}}entry")
    if entries:
        return [f for e in entries if (f := _parse_atom_entry(e)) is not None]

    return [f for item in root.findall(".//item") if (f := _parse_rss_item(item)) is not None]


def _parse_atom_entry(entry: ET.Element) -> dict[str, str] | None:
    """Extract filing metadata from an Atom <entry> element."""

    def _text(tag: str, ns: str = _EDGAR_NS) -> str:
        el = entry.find(f"{{{ns}}}{tag}")
        return el.text.strip() if el is not None and el.text else ""

    cik = _text("CIK") or _text("cik")
    accession = _text("accessionNumber") or _text("accession-number")
    form_type = _text("formType") or _text("form-type")
    date_filed = _text("dateFiled") or _text("date-filed")
    description = _text("summary", _ATOM_NS) or _text("title", _ATOM_NS)
    link_el = entry.find(f"{{{_ATOM_NS}}}link")
    link = link_el.get("href", "") if link_el is not None else ""

    if not accession or form_type.upper() != "8-K":
        return None

    return {
        "cik": cik,
        "accession": accession,
        "form_type": form_type,
        "date": date_filed,
        "description": description,
        "link": link,
    }


def _parse_rss_item(item: ET.Element) -> dict[str, str] | None:
    """Extract filing metadata from an RSS 2.0 <item> element."""

    def _text(tag: str, ns: str = _EDGAR_NS) -> str:
        el = item.find(f"{{{ns}}}{tag}")
        return el.text.strip() if el is not None and el.text else ""

    def _text_bare(tag: str) -> str:
        el = item.find(tag)
        return el.text.strip() if el is not None and el.text else ""

    cik = _text("CIK")
    accession = _text("accessionNumber")
    form_type = _text("formType")
    date_filed = _text("dateFiled")
    description = _text_bare("description")
    link = _text_bare("link")

    if not accession or form_type.upper() != "8-K":
        return None

    return {
        "cik": cik,
        "accession": accession,
        "form_type": form_type,
        "date": date_filed,
        "description": description,
        "link": link,
    }


def _article_exists(session_factory: Any, accession: str) -> bool:
    with session_factory() as sess:
        return (
            sess.execute(
                select(NewsArticles.article_id).where(NewsArticles.article_id == accession)
            ).scalar_one_or_none()
            is not None
        )


def _extract_item_codes(description: str) -> list[str]:
    """Return tags like ['8k', 'item_2.02'] extracted from a filing description."""
    return ["8k"] + [f"item_{m.group(1)}" for m in _ITEM_RE.finditer(description)]


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _fetch_and_store_body(
    client: SecEdgarClient,
    doc_url: str,
    accession: str,
    date_str: str,
    body_dir: Path,
) -> str | None:
    """Fetch the primary document, strip to text, write to disk. Returns path or None."""
    if not doc_url:
        return None
    try:
        plain = _strip_html(client.get(doc_url).text)
    except Exception:
        logger.warning("SEC EDGAR: failed to fetch body for %s", accession)
        return None

    year = date_str[:4] if len(date_str) >= 4 else "unknown"
    month = date_str[5:7] if len(date_str) >= 7 else "unknown"
    out_dir = body_dir / year / month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{accession}.txt"
    out_file.write_text(plain, encoding="utf-8")
    return str(out_file)


def _write_article(
    session_factory: Any,
    *,
    filing: dict[str, str],
    ticker: str,
    body_path: str | None,
) -> None:
    """Persist one ``news_articles`` row and its ``news_article_tickers`` row."""
    accession = filing["accession"]
    description = filing.get("description", "")
    date_str = filing.get("date", "")

    with session_factory() as sess:
        sess.add(
            NewsArticles(
                article_id=accession,
                source="sec_edgar_8k_rss",
                source_outlet="SEC EDGAR",
                language="en",
                headline_text=f"{ticker} 8-K filed {date_str} — {description}",
                body_path=body_path,
                published_at=date_str,
                ingested_at=datetime.now(UTC).isoformat(),
                topic_tags=json.dumps(_extract_item_codes(description)),
                url=filing.get("link") or None,
            )
        )
        sess.flush()  # satisfy news_article_tickers.article_id FK before insert

        sess.add(NewsArticleTickers(article_id=accession, ticker=ticker, is_primary=1))
        sess.commit()
