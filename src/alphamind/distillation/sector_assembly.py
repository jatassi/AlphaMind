"""Sector-scoped output assembly — story 02-distillation-layer/11a.

Produces the per-sector document each domain researcher agent
(``tech_semis`` / ``financials`` / ``energy``) consumes per
``docs/design/02-distillation-layer/external.md`` § Output format —
"Sector analyst agents: Their sector's tickers with full distillation
(indicators, anomalies, divergences)".

The assembly is a thin filter on top of stories 05 / 10 primitives:

- The sector roster is read from ``sector_classification.alphamind_sector``
  for the tickers whose ``domain_researcher`` matches the requested
  audience.
- Input blocks are filtered to those whose ``audience`` set contains
  the requested sector audience or :attr:`OutputAudience.UNIVERSAL_BROADCAST`.
- Per-ticker payload entries (``payload["per_ticker"][ticker]``) are
  restricted to the sector roster — a per-ticker block carrying only
  off-sector tickers is dropped entirely.
- The document framing is deterministic so the invocation archive
  (``docs/architecture/infrastructure.md`` § Invocation archive) diffs
  cleanly across runs.

The orchestrator (story 12) calls :func:`assemble_sector_output` three
times per invocation, once per sector audience.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.aggregation import (
    collect_anomalies,
    format_anomaly_summary,
    group_anomalies_by_audience,
)
from alphamind.distillation.output import (
    OutputAudience,
    OutputBlock,
    format_blocks_for_audience,
)
from alphamind.persistence.models import SectorClassification

# ---------------------------------------------------------------------------
# Audience → domain_researcher / sector_label mapping
# ---------------------------------------------------------------------------
#
# The story scopes per-sector output assembly to the three domain-researcher
# audiences. Centralizing the mapping here avoids the same lookup leaking
# into every caller — story-12's orchestrator iterates the keys, story-11a
# resolves both the storage-layer ``domain_researcher`` value and the
# human-readable label the document header carries.

DOMAIN_RESEARCHER_BY_AUDIENCE: Mapping[OutputAudience, str] = {
    OutputAudience.SECTOR_TECH_SEMIS: "tech_semis",
    OutputAudience.SECTOR_FINANCIALS: "financials",
    OutputAudience.SECTOR_ENERGY: "energy",
}
"""Sector audience → ``sector_classification.domain_researcher`` value.

The three keys are the only audiences :func:`assemble_sector_output`
accepts; the storage values match
``docs/design/01-data-layer/collector/storage.md`` § ``sector_classification``.
"""


SECTOR_LABEL_BY_AUDIENCE: Mapping[OutputAudience, str] = {
    OutputAudience.SECTOR_TECH_SEMIS: "Tech & Semis",
    OutputAudience.SECTOR_FINANCIALS: "Financials",
    OutputAudience.SECTOR_ENERGY: "Energy",
}
"""Sector audience → human-readable label rendered in the document header.

The labels match the ``Sector brief: {sector_label}`` template in
``docs/design/03-analysis-layer/domain-researchers/tech-semis.md``
§ Domain researcher output contract — the sector researcher echoes the
same label in its own output, so the label is part of the
researcher-facing contract.
"""


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectorOutput:
    """The assembled per-sector document.

    Carries the rendered ``text`` plus enough metadata for the orchestrator
    (story 12) to persist the document to the brief store and the
    invocation archive without re-deriving fields.

    - ``audience`` — one of the three sector audiences in
      :data:`DOMAIN_RESEARCHER_BY_AUDIENCE`.
    - ``sector_label`` — the human-readable label the domain researcher
      echoes in its own output.
    - ``text`` — the assembled document body.
    - ``tickers`` — sector roster, alphabetical, deterministic across
      repeated calls.
    - ``block_ids`` — every block id that survived the filter (audit
      handle for the orchestrator and the replay harness).
    - ``freshness_min`` — oldest ``freshness_ts`` of any included block;
      the conservative document freshness downstream agents read.
    """

    audience: OutputAudience
    sector_label: str
    text: str
    tickers: tuple[str, ...]
    block_ids: tuple[str, ...]
    freshness_min: datetime


# ---------------------------------------------------------------------------
# Sector roster lookup
# ---------------------------------------------------------------------------


def load_sector_roster(session: Session, domain_researcher: str) -> tuple[str, ...]:
    """Return the sorted ticker roster for a ``domain_researcher`` value.

    The roster is the set of tickers whose ``sector_classification``
    row carries the requested ``domain_researcher`` — the storage spec
    pairs each sector audience with one storage-layer value (story
    file § ``sector_classification`` — "tech_semis / financials / energy").

    The orchestrator pre-computes the roster once per audience in the main
    thread and passes the result to :func:`assemble_sector_output`. The
    SQLAlchemy ``Session`` is not thread-safe under concurrent reads, so
    the DB lookup must happen before any ``asyncio.gather`` fan-out.
    """
    rows = session.execute(
        select(SectorClassification.ticker).where(
            SectorClassification.domain_researcher == domain_researcher
        )
    ).all()
    return tuple(sorted(row[0] for row in rows))


# ---------------------------------------------------------------------------
# Per-ticker payload filtering
# ---------------------------------------------------------------------------
#
# Stories 08* emit per-ticker blocks whose payload follows the convention
# ``payload["per_ticker"][ticker] = {...}``. The sector filter inspects
# this key path and prunes any ticker that isn't in the sector roster.
# Blocks without a ``per_ticker`` payload key pass through unchanged —
# they are aggregate / non-ticker-keyed blocks (e.g. ``regime.label``).

_PER_TICKER_KEY = "per_ticker"


def _restrict_per_ticker_payload(block: OutputBlock, roster: frozenset[str]) -> OutputBlock | None:
    """Return ``block`` with off-roster tickers pruned, or ``None`` if empty.

    A block whose payload has no ``per_ticker`` entry passes through
    unchanged. A per-ticker block whose tickers are all off-roster is
    dropped (returns ``None``) — emitting an empty per-ticker dict
    would clutter the document with no signal.
    """
    payload = block.payload
    raw = payload.get(_PER_TICKER_KEY)
    if not isinstance(raw, Mapping):
        return block
    kept = {ticker: value for ticker, value in raw.items() if ticker in roster}
    if not kept:
        return None
    if kept.keys() == raw.keys():
        return block
    new_payload: dict[str, object] = dict(payload)
    new_payload[_PER_TICKER_KEY] = dict(sorted(kept.items()))
    return OutputBlock(
        block_id=block.block_id,
        audience=block.audience,
        freshness_ts=block.freshness_ts,
        calibration_state=block.calibration_state,
        bootstrap_reason=block.bootstrap_reason,
        payload=new_payload,
        anomaly_flags=block.anomaly_flags,
        regime_context=block.regime_context,
    )


# ---------------------------------------------------------------------------
# Document framing
# ---------------------------------------------------------------------------
#
# The framing must be deterministic so the invocation archive diffs cleanly
# (per ``docs/architecture/infrastructure.md`` § Invocation archive). Every
# input is sorted before rendering and no wall-clock value is embedded.
#
# Section order matches the recommended layout in the story file:
#
# 1. Header (sector label, invocation id, ticker roster, effective
#    freshness).
# 2. Anomaly summary at top — most actionable first per
#    ``docs/design/02-distillation-layer/external.md`` § Output format.
# 3. Universal context — macro / regime / breadth that frames
#    interpretation.
# 4. Sector indicators — deepest detail last.

_EPOCH_ZERO_UTC = datetime.fromtimestamp(0, tz=UTC)
"""Sentinel ``freshness_min`` for the empty-block case.

A document carrying no blocks has no observed freshness; the conservative
sentinel is epoch zero so any downstream "is this older than X" check
treats the empty document as fully stale.
"""


def _format_header(
    *,
    sector_label: str,
    invocation_id: str,
    tickers: tuple[str, ...],
    freshness_min: datetime,
) -> str:
    """Render the document header per the story's framing template."""
    ticker_list = ", ".join(tickers) if tickers else "(none)"
    lines = [
        f"DISTILLATION OUTPUT — {sector_label}",
        f"Invocation: {invocation_id}",
        f"Tickers: {ticker_list}",
        f"Effective freshness: {freshness_min.isoformat()}",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def assemble_sector_output(
    *,
    audience: OutputAudience,
    blocks: Iterable[OutputBlock],
    roster: tuple[str, ...],
    invocation_id: str,
) -> SectorOutput:
    """Assemble the per-sector distillation document for ``audience``.

    Filtering rules (per the story file):

    1. The sector's ticker ``roster`` is supplied by the caller (the
       orchestrator pre-computes it via :func:`load_sector_roster` in the
       main thread before fanning out to the per-audience workers).
    2. Keep blocks whose ``audience`` set contains either ``audience``
       itself or :attr:`OutputAudience.UNIVERSAL_BROADCAST`.
    3. Within the kept set, restrict ``payload["per_ticker"]`` entries to
       roster tickers. Drop per-ticker blocks left empty.
    4. Render the document deterministically (header → anomaly summary →
       universal context → sector indicators).

    Raises :class:`ValueError` for an audience that isn't one of the three
    sector audiences.
    """
    if audience not in DOMAIN_RESEARCHER_BY_AUDIENCE:
        raise ValueError(
            f"assemble_sector_output: {audience!r} is not a sector audience; "
            f"expected one of {sorted(a.value for a in DOMAIN_RESEARCHER_BY_AUDIENCE)}"
        )

    sector_label = SECTOR_LABEL_BY_AUDIENCE[audience]
    tickers = roster
    roster_set = frozenset(tickers)

    surviving: list[OutputBlock] = []
    for block in blocks:
        if not (audience in block.audience or OutputAudience.UNIVERSAL_BROADCAST in block.audience):
            continue
        restricted = _restrict_per_ticker_payload(block, roster_set)
        if restricted is None:
            continue
        surviving.append(restricted)

    surviving.sort(key=lambda block: block.block_id)

    universal_blocks = [
        block for block in surviving if OutputAudience.UNIVERSAL_BROADCAST in block.audience
    ]
    sector_blocks = [block for block in surviving if audience in block.audience]

    freshness_min = min(block.freshness_ts for block in surviving) if surviving else _EPOCH_ZERO_UTC

    grouped_anomalies = group_anomalies_by_audience(collect_anomalies(surviving))
    anomaly_summary = format_anomaly_summary(grouped_anomalies.get(audience, []))
    universal_text = format_blocks_for_audience(
        universal_blocks, OutputAudience.UNIVERSAL_BROADCAST
    )
    sector_text = format_blocks_for_audience(sector_blocks, audience)

    body_parts = [
        _format_header(
            sector_label=sector_label,
            invocation_id=invocation_id,
            tickers=tickers,
            freshness_min=freshness_min,
        ),
        anomaly_summary,
        "",
        "=== UNIVERSAL CONTEXT ===",
        "",
        universal_text or "(no universal-broadcast blocks)\n",
        "",
        "=== SECTOR INDICATORS ===",
        "",
        sector_text or "(no sector indicator blocks)\n",
    ]
    text = "\n".join(body_parts)

    return SectorOutput(
        audience=audience,
        sector_label=sector_label,
        text=text,
        tickers=tickers,
        block_ids=tuple(block.block_id for block in surviving),
        freshness_min=freshness_min,
    )


__all__ = [
    "DOMAIN_RESEARCHER_BY_AUDIENCE",
    "SECTOR_LABEL_BY_AUDIENCE",
    "SectorOutput",
    "assemble_sector_output",
    "load_sector_roster",
]
