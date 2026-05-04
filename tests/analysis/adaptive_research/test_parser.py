"""Tests for adaptive-research brief parser — ALP-257.

Covers all acceptance criteria from the story:
  AC-1  Import resolves cleanly.
  AC-2  Two-thread (signal + inconclusive) brief parses.
  AC-3  Zero-thread brief with empty INVESTIGATION THREADS section parses.
  AC-4  Multi-element anomalies_deferred parses with whitespace stripped.
  AC-5  Out-of-sequence thread (AR-1, AR-3) raises ParseError on threads[1].thread_id.
  AC-6  Signal thread missing Strengthens raises ParseError on threads[0].strengthens.
  AC-7  Noise thread carrying Implication raises ParseError (model invariant surfaces).
  AC-8  Invalid Sector value raises ParseError on threads[0].sector.
  AC-9  Invalid Assessment value raises ParseError.
  AC-10 Invocation_id mismatch raises ParseError on header.invocation_id.
  AC-11 Missing INVESTIGATION THREADS marker raises ParseError on
        header.investigation_threads_marker.
  AC-12 Markdown fences (bare and ```text) are stripped.
  AC-13 Verbatim example output parses.
  AC-14 Round-trip: parse → render → parse again gives structural equality.
"""

from __future__ import annotations

import pytest

from alphamind.analysis.adaptive_research.parser import ParseError, parse_adaptive_brief

# ---------------------------------------------------------------------------
# Verbatim example from prompts/analysis/adaptive_researcher.md § <example_output>
# ---------------------------------------------------------------------------

EXAMPLE_OUTPUT = """\
ADAPTIVE RESEARCH FINDINGS
Invocation: inv-2026-04-23T14-30Z
Threads investigated: 2 of 3 anomalies triaged
Anomalies deferred: Distillation: gold-yield correlation flip, persistence 2 sessions

=== INVESTIGATION THREADS ===
[AR-1]
  Trigger: [SA-TECH-ANOM-1] (also Distillation: NVDA volume 3.2σ over 5-day average)
  Question: What news or positioning drove NVDA's volume spike during the past two sessions in the absence of a price move?
  Tickers: NVDA
  Sector: tech_semis
  Tools used: news_search, options_flow, ticker_deep_pull
  Findings:
    - news_search "NVDA institutional positioning" returned three sell-side notes published in the past 36 hours flagging pre-earnings reposition
    - options_flow on NVDA shows directional bias to upside calls 2.1× recent baseline; skew unchanged
    - ticker_deep_pull short-interest figures on NVDA: short interest declined 1.2% over the same window — consistent with covering, not new bearish positioning
  Assessment: signal
  Confidence: moderate
  Implication: The volume spike is consistent with pre-earnings institutional repositioning rather than information asymmetry on fundamentals. The earnings print inside 30 hours is the resolution event; flow shape suggests positioning conviction but not extreme conviction.
  Strengthens: [SA-TECH-2]
  Weakens: none

[AR-2]
  Trigger: [SA-ENERGY-ANOM-1] (refining correlation break)
  Question: Is the refining-group correlation break driven by a single-name catalyst or by a sector-wide shift in crack-spread expectations?
  Tickers: VLO, MPC, PSX
  Sector: energy
  Tools used: news_search, macro_data
  Findings:
    - news_search "refining outage" returned no recent unplanned-outage headlines on VLO/MPC/PSX
    - macro_data crack_spread series: 5-day spread widened ~$2 with no single-day shock — gradual, not event-driven
  Assessment: inconclusive
  Confidence: low
  Missing: Per-name capacity-utilization data for the past two weeks; whether the spread widening is durable or seasonal calibration. The available tools cannot resolve which interpretation is correct without next-cycle observation.
"""

EXAMPLE_INVOCATION_ID = "inv-2026-04-23T14-30Z"


# ---------------------------------------------------------------------------
# Helpers — minimal valid brief shapes
# ---------------------------------------------------------------------------


def _empty_brief(invocation_id: str = "inv-test") -> str:
    return (
        "ADAPTIVE RESEARCH FINDINGS\n"
        f"Invocation: {invocation_id}\n"
        "Threads investigated: 0 of 0 anomalies triaged\n"
        "Anomalies deferred: none\n"
        "\n"
        "=== INVESTIGATION THREADS ===\n"
    )


def _signal_thread(num: int = 1) -> str:
    return (
        f"[AR-{num}]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove the volume spike?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search, options_flow\n"
        "  Findings:\n"
        "    - news_search returned pre-earnings notes\n"
        "    - options_flow shows upside bias\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: Pre-earnings repositioning.\n"
        "  Strengthens: [SA-TECH-2]\n"
        "  Weakens: none\n"
    )


def _inconclusive_thread(num: int = 2) -> str:
    return (
        f"[AR-{num}]\n"
        "  Trigger: [SA-ENERGY-ANOM-1]\n"
        "  Question: Is this a sector-wide shift?\n"
        "  Tickers: VLO, MPC, PSX\n"
        "  Sector: energy\n"
        "  Tools used: news_search, macro_data\n"
        "  Findings:\n"
        "    - news_search returned no outage headlines\n"
        "    - macro_data crack spread widened\n"
        "  Assessment: inconclusive\n"
        "  Confidence: low\n"
        "  Missing: Per-name capacity-utilization data.\n"
    )


def _noise_thread(num: int = 1) -> str:
    return (
        f"[AR-{num}]\n"
        "  Trigger: Distillation: minor blip\n"
        "  Question: Is this signal?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned routine commentary\n"
        "  Assessment: noise\n"
        "  Confidence: high\n"
        "  Dismissal reason: Routine reposition with no anomalous catalyst.\n"
    )


def _brief_with(*threads: str, invocation_id: str = "inv-test", triaged: int = 2) -> str:
    return (
        "ADAPTIVE RESEARCH FINDINGS\n"
        f"Invocation: {invocation_id}\n"
        f"Threads investigated: {len(threads)} of {triaged} anomalies triaged\n"
        "Anomalies deferred: none\n"
        "\n"
        "=== INVESTIGATION THREADS ===\n" + "\n".join(threads)
    )


# ---------------------------------------------------------------------------
# AC-1: Import resolves
# ---------------------------------------------------------------------------


def test_imports_resolve() -> None:
    """ParseError and parse_adaptive_brief are importable."""
    assert callable(parse_adaptive_brief)
    assert issubclass(ParseError, Exception)
    assert hasattr(ParseError("f", "m"), "field_path")
    assert hasattr(ParseError("f", "m"), "message")


# ---------------------------------------------------------------------------
# AC-2: two-thread (signal + inconclusive) brief parses to threads of length 2
# ---------------------------------------------------------------------------


def test_two_thread_brief_parses() -> None:
    """A well-formed brief with one signal and one inconclusive thread parses."""
    text = _brief_with(_signal_thread(1), _inconclusive_thread(2))
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert len(brief.threads) == 2
    signal, inconclusive = brief.threads
    assert signal.assessment.value == "signal"
    assert signal.implication is not None
    assert signal.strengthens == ("SA-TECH-2",)
    assert signal.weakens == ()
    assert signal.dismissal_reason is None
    assert signal.missing is None
    assert inconclusive.assessment.value == "inconclusive"
    assert inconclusive.missing is not None
    assert inconclusive.implication is None


# ---------------------------------------------------------------------------
# AC-3: zero-thread brief with empty INVESTIGATION THREADS section parses
# ---------------------------------------------------------------------------


def test_zero_thread_brief_parses() -> None:
    """A brief with 'Threads investigated: 0 of 0' and empty section parses."""
    brief = parse_adaptive_brief(_empty_brief(), invocation_id="inv-test")
    assert brief.threads == ()
    assert brief.anomalies_deferred == ()
    assert brief.threads_investigated_count == 0
    assert brief.anomalies_triaged_count == 0


# ---------------------------------------------------------------------------
# AC-4: multi-element Anomalies deferred parses with whitespace stripped
# ---------------------------------------------------------------------------


def test_anomalies_deferred_multi_element_whitespace_stripped() -> None:
    """A comma-separated `Anomalies deferred:` value yields a tuple with
    whitespace-stripped entries."""
    text = (
        "ADAPTIVE RESEARCH FINDINGS\n"
        "Invocation: inv-test\n"
        "Threads investigated: 0 of 2 anomalies triaged\n"
        "Anomalies deferred: SA-TECH-ANOM-1, Distillation: q1.volume_spike NVDA 3.2σ\n"
        "\n"
        "=== INVESTIGATION THREADS ===\n"
    )
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.anomalies_deferred == (
        "SA-TECH-ANOM-1",
        "Distillation: q1.volume_spike NVDA 3.2σ",
    )


# ---------------------------------------------------------------------------
# AC-5: out-of-sequence threads raise ParseError on threads[1].thread_id
# ---------------------------------------------------------------------------


def test_out_of_sequence_thread_id_raises() -> None:
    """[AR-1] then [AR-3] raises ParseError naming threads[1].thread_id."""
    text = _brief_with(_signal_thread(1), _inconclusive_thread(3))
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[1].thread_id"


# ---------------------------------------------------------------------------
# AC-6: signal thread missing Strengthens raises ParseError
# ---------------------------------------------------------------------------


def test_signal_thread_missing_strengthens_raises() -> None:
    """A signal thread without `Strengthens:` raises ParseError on threads[0].strengthens."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove it?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned notes\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: Pre-earnings repositioning.\n"
        "  Weakens: none\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].strengthens"


# ---------------------------------------------------------------------------
# AC-7: noise thread carrying Implication raises ParseError (model invariant)
# ---------------------------------------------------------------------------


def test_noise_thread_with_implication_raises() -> None:
    """A noise thread that carries an Implication: line raises ParseError —
    the model invariant rejects, parser surfaces."""
    thread = (
        "[AR-1]\n"
        "  Trigger: Distillation: minor blip\n"
        "  Question: Is this signal?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned routine commentary\n"
        "  Assessment: noise\n"
        "  Confidence: high\n"
        "  Dismissal reason: Routine.\n"
        "  Implication: This should not be here.\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError):
        parse_adaptive_brief(text, invocation_id="inv-test")


# ---------------------------------------------------------------------------
# AC-8: invalid Sector value raises ParseError on threads[0].sector
# ---------------------------------------------------------------------------


def test_invalid_sector_value_raises() -> None:
    """`Sector: macro` raises ParseError on threads[0].sector."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove it?\n"
        "  Tickers: NVDA\n"
        "  Sector: macro\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned notes\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: x.\n"
        "  Strengthens: none\n"
        "  Weakens: none\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].sector"


# ---------------------------------------------------------------------------
# AC-9: invalid Assessment value raises ParseError
# ---------------------------------------------------------------------------


def test_invalid_assessment_value_raises() -> None:
    """`Assessment: maybe` raises ParseError."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove it?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned notes\n"
        "  Assessment: maybe\n"
        "  Confidence: moderate\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert "assessment" in exc_info.value.field_path.lower()


# ---------------------------------------------------------------------------
# AC-10: invocation_id drift is tolerated; caller-supplied id wins
# ---------------------------------------------------------------------------


def test_invocation_id_mismatch_is_tolerated() -> None:
    """Model drift on the `Invocation:` line is tolerated — the caller-supplied
    id is canonical and is set on the returned brief. Mirrors the qualitative
    parser's discard-and-overwrite behavior; loosened in ALP-271 because Sonnet
    occasionally invents its own id under load and the strict check rejected
    otherwise-valid briefs."""
    text = _empty_brief(invocation_id="inv-actual")
    brief = parse_adaptive_brief(text, invocation_id="inv-different")
    assert brief.invocation_id == "inv-different"


# ---------------------------------------------------------------------------
# AC-11: missing INVESTIGATION THREADS marker raises ParseError
# ---------------------------------------------------------------------------


def test_missing_investigation_threads_marker_raises() -> None:
    """Brief without `=== INVESTIGATION THREADS ===` raises ParseError on
    header.investigation_threads_marker."""
    text = (
        "ADAPTIVE RESEARCH FINDINGS\n"
        "Invocation: inv-test\n"
        "Threads investigated: 0 of 0 anomalies triaged\n"
        "Anomalies deferred: none\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "header.investigation_threads_marker"


# ---------------------------------------------------------------------------
# AC-12: markdown fences (bare and ```text) are stripped
# ---------------------------------------------------------------------------


def test_bare_fence_stripped() -> None:
    """A plain ``` fence is stripped before parsing."""
    fenced = "```\n" + _empty_brief().rstrip() + "\n```"
    brief = parse_adaptive_brief(fenced, invocation_id="inv-test")
    assert brief.threads == ()


def test_text_fence_stripped() -> None:
    """A ```text fence is stripped before parsing."""
    fenced = "```text\n" + _empty_brief().rstrip() + "\n```"
    brief = parse_adaptive_brief(fenced, invocation_id="inv-test")
    assert brief.threads == ()


# ---------------------------------------------------------------------------
# AC-13: verbatim example from prompt parses
# ---------------------------------------------------------------------------


def test_example_output_parses() -> None:
    """Verbatim worked example from prompts/analysis/adaptive_researcher.md parses."""
    brief = parse_adaptive_brief(EXAMPLE_OUTPUT, invocation_id=EXAMPLE_INVOCATION_ID)
    assert len(brief.threads) == 2
    assessments = {t.assessment.value for t in brief.threads}
    assert assessments == {"signal", "inconclusive"}
    assert brief.threads_investigated_count == 2
    assert brief.anomalies_triaged_count == 3
    # The spec splits `Anomalies deferred:` on commas; the example's deferred
    # description happens to contain one comma, yielding two elements.
    assert brief.anomalies_deferred == (
        "Distillation: gold-yield correlation flip",
        "persistence 2 sessions",
    )


# ---------------------------------------------------------------------------
# AC-14: round-trip: parse → render → parse → structural equality
# ---------------------------------------------------------------------------


def _render_brief(brief: object) -> str:
    """Re-render an AdaptiveBrief back to its wire format. Test-only.

    Mirrors the prompt's <output_contract> exactly. Intentionally minimal —
    enough fidelity for a round-trip equality check.
    """
    from alphamind.analysis.adaptive_research.models import AdaptiveBrief

    assert isinstance(brief, AdaptiveBrief)

    deferred_repr = ", ".join(brief.anomalies_deferred) if brief.anomalies_deferred else "none"
    out = [
        "ADAPTIVE RESEARCH FINDINGS",
        f"Invocation: {brief.invocation_id}",
        (
            f"Threads investigated: {brief.threads_investigated_count} of "
            f"{brief.anomalies_triaged_count} anomalies triaged"
        ),
        f"Anomalies deferred: {deferred_repr}",
        "",
        "=== INVESTIGATION THREADS ===",
    ]
    for t in brief.threads:
        tickers_repr = ", ".join(t.tickers) if t.tickers else "none"
        tools_repr = ", ".join(t.tools_used) if t.tools_used else "none"
        out.extend(
            [
                f"[{t.thread_id}]",
                f"  Trigger: {t.trigger}",
                f"  Question: {t.question}",
                f"  Tickers: {tickers_repr}",
                f"  Sector: {t.sector.value}",
                f"  Tools used: {tools_repr}",
                "  Findings:",
            ]
        )
        for f in t.findings:
            out.append(f"    - {f}")
        out.append(f"  Assessment: {t.assessment.value}")
        out.append(f"  Confidence: {t.confidence.value}")
        if t.assessment.value == "signal":
            assert t.implication is not None
            assert t.strengthens is not None
            assert t.weakens is not None
            out.append(f"  Implication: {t.implication}")
            # Brackets are wire syntax; the parser strips them and the
            # validator universe stores unbracketed IDs, so re-add brackets
            # here when serializing back to wire format.
            strengthens_repr = (
                ", ".join(f"[{r}]" for r in t.strengthens) if t.strengthens else "none"
            )
            weakens_repr = ", ".join(f"[{r}]" for r in t.weakens) if t.weakens else "none"
            out.append(f"  Strengthens: {strengthens_repr}")
            out.append(f"  Weakens: {weakens_repr}")
        elif t.assessment.value == "noise":
            assert t.dismissal_reason is not None
            out.append(f"  Dismissal reason: {t.dismissal_reason}")
        else:
            assert t.missing is not None
            out.append(f"  Missing: {t.missing}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def test_round_trip_structural_equality() -> None:
    """parse → render → parse again gives a structurally-equal AdaptiveBrief."""
    first = parse_adaptive_brief(EXAMPLE_OUTPUT, invocation_id=EXAMPLE_INVOCATION_ID)
    rendered = _render_brief(first)
    second = parse_adaptive_brief(rendered, invocation_id=EXAMPLE_INVOCATION_ID)
    assert first.model_dump() == second.model_dump()


# ---------------------------------------------------------------------------
# Preamble tolerance — text emitted between tool calls is concatenated into
# the response by the harness, so the parser tolerates lines preceding the
# `ADAPTIVE RESEARCH FINDINGS` header. Discovered during ALP-287 end-to-end
# verification: a substantive 6-thread brief was rejected because the agent
# emitted intra-tool-call narration ahead of the header.
# ---------------------------------------------------------------------------


def test_single_line_preamble_before_header_is_tolerated() -> None:
    """A single narration line preceding the header is discarded; brief parses."""
    text = "Good. All tools have returned results. Emitting the brief now.\n" + _empty_brief()
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.threads == ()
    assert brief.threads_investigated_count == 0


def test_multi_paragraph_preamble_before_header_is_tolerated() -> None:
    """Multiple narration paragraphs before the header are discarded; brief parses."""
    text = (
        "Now I'll execute the first wave of parallel research calls.\n"
        "News feed unavailable; pivoting to remaining tools.\n"
        "All tools returned. Sufficient data to verdict.\n"
        "\n"
        "---\n"
        "\n" + _brief_with(_signal_thread(1), _inconclusive_thread(2))
    )
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert len(brief.threads) == 2
    assert brief.threads[0].assessment.value == "signal"


def test_absent_header_still_raises() -> None:
    """Preamble tolerance does not weaken the absent-header check."""
    text = (
        "I investigated three threads but forgot the header.\n"
        "[AR-1]\n  Trigger: foo\n  Question: bar\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "envelope"
    assert "ADAPTIVE RESEARCH FINDINGS" in exc_info.value.message


# ---------------------------------------------------------------------------
# Counts-line tolerance — Sonnet has been observed to write
# "anomaly groups triaged" instead of "anomalies triaged" and append a
# parenthetical explanation. The regex extracts the two integers and
# tolerates these natural variations as long as "triaged" appears.
# ---------------------------------------------------------------------------


def test_counts_line_accepts_anomaly_groups_phrasing() -> None:
    """`0 of 10 anomaly groups triaged` is accepted; the two counts parse."""
    text = (
        "ADAPTIVE RESEARCH FINDINGS\n"
        "Invocation: inv-test\n"
        "Threads investigated: 0 of 10 anomaly groups triaged\n"
        "Anomalies deferred: none\n"
        "\n"
        "=== INVESTIGATION THREADS ===\n"
    )
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.threads_investigated_count == 0
    assert brief.anomalies_triaged_count == 10


def test_counts_line_accepts_trailing_parenthetical() -> None:
    """Trailing parenthetical commentary after `triaged` is accepted."""
    text = (
        "ADAPTIVE RESEARCH FINDINGS\n"
        "Invocation: inv-test\n"
        "Threads investigated: 0 of 10 anomalies triaged "
        "(52 distillation flags consolidated into 4 logical clusters)\n"
        "Anomalies deferred: none\n"
        "\n"
        "=== INVESTIGATION THREADS ===\n"
    )
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.threads_investigated_count == 0
    assert brief.anomalies_triaged_count == 10


def test_counts_line_without_triaged_still_raises() -> None:
    """The regex still anchors on `triaged`; absent token raises ParseError."""
    text = (
        "ADAPTIVE RESEARCH FINDINGS\n"
        "Invocation: inv-test\n"
        "Threads investigated: 0 of 10 anomalies inspected\n"
        "Anomalies deferred: none\n"
        "\n"
        "=== INVESTIGATION THREADS ===\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "header.threads_investigated_count"


# ---------------------------------------------------------------------------
# Marker scan tolerance — Sonnet has been observed to insert auxiliary
# "Note:" prose between `Anomalies deferred:` and the threads section
# marker. The parser scans forward to the marker rather than requiring it
# on the next non-blank line.
# ---------------------------------------------------------------------------


def test_marker_scan_skips_intervening_note_lines() -> None:
    """Auxiliary `Note:` lines between header and marker are tolerated."""
    text = (
        "ADAPTIVE RESEARCH FINDINGS\n"
        "Invocation: inv-test\n"
        "Threads investigated: 0 of 3 anomalies triaged\n"
        "Anomalies deferred: none\n"
        "Note: news_search universally unavailable this cycle.\n"
        "Note: short_interest metrics null across all queried tickers.\n"
        "\n"
        "=== INVESTIGATION THREADS ===\n"
    )
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.threads_investigated_count == 0
    assert brief.anomalies_triaged_count == 3


# ---------------------------------------------------------------------------
# Strengthens/Weakens reference extraction — Sonnet has been observed to
# append free-text rationale after each bracketed reference. The parser
# extracts only the bracketed IDs and discards the trailing commentary.
# ---------------------------------------------------------------------------


def test_strengthens_with_trailing_commentary_extracts_id() -> None:
    """`Strengthens: [SA-TECH-1] (commentary)` parses to the bare ID."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove the volume spike?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned pre-earnings notes\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: Pre-earnings repositioning.\n"
        "  Strengthens: [SA-TECH-1] (the move shares a common macro driver)\n"
        "  Weakens: [SA-FIN-2] (V's neutral volume argues against this)\n"
    )
    text = _brief_with(thread, triaged=1)
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.threads[0].strengthens == ("SA-TECH-1",)
    assert brief.threads[0].weakens == ("SA-FIN-2",)


def test_strengthens_multiple_refs_with_commentary() -> None:
    """Multiple bracketed refs separated by commentary are all extracted."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove the volume spike?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned pre-earnings notes\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: Pre-earnings repositioning.\n"
        "  Strengthens: [SA-TECH-1] (note A) and [SA-TECH-2] (note B)\n"
        "  Weakens: none\n"
    )
    text = _brief_with(thread, triaged=1)
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.threads[0].strengthens == ("SA-TECH-1", "SA-TECH-2")
    assert brief.threads[0].weakens == ()


# ---------------------------------------------------------------------------
# Tools used — Sonnet has been observed to attach the ticker its tool call
# targeted in parentheses (e.g., `ticker_deep_pull (COP)`). The parser
# strips trailing "(...)" commentary so the validator's allowlist match
# sees the bare tool name.
# ---------------------------------------------------------------------------


def test_sector_abbreviation_aliases_resolve() -> None:
    """`Sector: tech` resolves to `tech_semis`; `fin` resolves to `financials`."""
    thread_tech = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove the volume spike?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned pre-earnings notes\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: Pre-earnings repositioning.\n"
        "  Strengthens: [SA-TECH-1]\n"
        "  Weakens: none\n"
    )
    text = _brief_with(thread_tech, triaged=1)
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    assert brief.threads[0].sector.value == "tech_semis"


def test_tools_used_with_ticker_in_parens_is_stripped() -> None:
    """`ticker_deep_pull (COP)` parses to bare `ticker_deep_pull`."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-ENERGY-ANOM-1]\n"
        "  Question: Refining-margin shift?\n"
        "  Tickers: COP, MPC\n"
        "  Sector: energy\n"
        "  Tools used: ticker_deep_pull (COP), news_search, ticker_deep_pull (MPC)\n"
        "  Findings:\n"
        "    - ticker_deep_pull returned utilization detail\n"
        "  Assessment: inconclusive\n"
        "  Confidence: low\n"
        "  Missing: capacity-utilization data\n"
    )
    text = _brief_with(thread, triaged=1)
    brief = parse_adaptive_brief(text, invocation_id="inv-test")
    # Repeated `ticker_deep_pull` calls (with different targets in the parens)
    # collapse to a single entry — the validator's distinct-within-thread
    # check would otherwise reject the brief, and `tools_used` semantically
    # describes which tools the thread invoked, not call counts.
    assert brief.threads[0].tools_used == ("ticker_deep_pull", "news_search")


# ---------------------------------------------------------------------------
# Forbidden-conditional-field parser checks — clearer ParseError than the
# pydantic-validator-rewrap path. Each forbidden field gets its own branch.
# ---------------------------------------------------------------------------


_FORBIDDEN_MESSAGE = {
    "strengthens": "strengthens is only valid for signal threads",
    "weakens": "weakens is only valid for signal threads",
    "implication": "implication is only valid for signal threads",
    "dismissal_reason": "dismissal_reason is only valid for noise threads",
    "missing": "missing is only valid for inconclusive threads",
}


def test_noise_thread_with_strengthens_raises_clear_parse_error() -> None:
    """A NOISE thread carrying `Strengthens:` raises ParseError with a clear message."""
    thread = (
        "[AR-1]\n"
        "  Trigger: Distillation: minor blip\n"
        "  Question: Is this signal?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned routine commentary\n"
        "  Assessment: noise\n"
        "  Confidence: high\n"
        "  Dismissal reason: Routine.\n"
        "  Strengthens: none\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].strengthens"
    assert exc_info.value.message == _FORBIDDEN_MESSAGE["strengthens"]


def test_noise_thread_with_weakens_raises_clear_parse_error() -> None:
    """A NOISE thread carrying `Weakens:` raises ParseError naming threads[0].weakens."""
    thread = (
        "[AR-1]\n"
        "  Trigger: Distillation: minor blip\n"
        "  Question: Is this signal?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned routine commentary\n"
        "  Assessment: noise\n"
        "  Confidence: high\n"
        "  Dismissal reason: Routine.\n"
        "  Weakens: none\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].weakens"
    assert exc_info.value.message == _FORBIDDEN_MESSAGE["weakens"]


def test_inconclusive_thread_with_implication_raises_clear_parse_error() -> None:
    """An INCONCLUSIVE thread with `Implication:` raises ParseError naming threads[0].implication."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-ENERGY-ANOM-1]\n"
        "  Question: Is this a sector-wide shift?\n"
        "  Tickers: VLO\n"
        "  Sector: energy\n"
        "  Tools used: macro_data\n"
        "  Findings:\n"
        "    - macro_data crack spread widened\n"
        "  Assessment: inconclusive\n"
        "  Confidence: low\n"
        "  Missing: Per-name capacity-utilization data.\n"
        "  Implication: This should not be here.\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].implication"
    assert exc_info.value.message == _FORBIDDEN_MESSAGE["implication"]


def test_signal_thread_with_dismissal_reason_raises_clear_parse_error() -> None:
    """A SIGNAL thread carrying `Dismissal reason:` raises ParseError naming threads[0]."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove it?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned notes\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: x.\n"
        "  Strengthens: [SA-TECH-1]\n"
        "  Weakens: none\n"
        "  Dismissal reason: spurious.\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].dismissal_reason"
    assert exc_info.value.message == _FORBIDDEN_MESSAGE["dismissal_reason"]


def test_signal_thread_with_missing_raises_clear_parse_error() -> None:
    """A SIGNAL thread carrying `Missing:` raises ParseError naming threads[0].missing."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-TECH-ANOM-1]\n"
        "  Question: What drove it?\n"
        "  Tickers: NVDA\n"
        "  Sector: tech_semis\n"
        "  Tools used: news_search\n"
        "  Findings:\n"
        "    - news_search returned notes\n"
        "  Assessment: signal\n"
        "  Confidence: moderate\n"
        "  Implication: x.\n"
        "  Strengthens: [SA-TECH-1]\n"
        "  Weakens: none\n"
        "  Missing: spurious.\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].missing"
    assert exc_info.value.message == _FORBIDDEN_MESSAGE["missing"]


def test_inconclusive_thread_with_dismissal_reason_raises_clear_parse_error() -> None:
    """An INCONCLUSIVE thread with `Dismissal reason:` raises a clear ParseError."""
    thread = (
        "[AR-1]\n"
        "  Trigger: [SA-ENERGY-ANOM-1]\n"
        "  Question: Is this a sector-wide shift?\n"
        "  Tickers: VLO\n"
        "  Sector: energy\n"
        "  Tools used: macro_data\n"
        "  Findings:\n"
        "    - macro_data crack spread widened\n"
        "  Assessment: inconclusive\n"
        "  Confidence: low\n"
        "  Missing: Per-name data.\n"
        "  Dismissal reason: spurious.\n"
    )
    text = _brief_with(thread, triaged=1)
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(text, invocation_id="inv-test")
    assert exc_info.value.field_path == "threads[0].dismissal_reason"
    assert exc_info.value.message == _FORBIDDEN_MESSAGE["dismissal_reason"]


# Existing test_noise_thread_with_implication_raises continues to validate the
# noise+implication combination — that test now exercises the same parser-level
# branch as the new ones.
