"""Post-refactor architecture invariants from the 2026-05-12 audit (ALP-454).

These tests act as regression guards for the headline findings in
``audit-alphamind-2026-05-12.html``. They assert ranges/sets rather than
exact counts so unrelated future work doesn't churn this file — but any
substantial drift from the post-refactor baseline trips a test.

Tests verify:

* **Cycle elimination (L2, L3 in the audit).** The two system-spanning
  cycles (10-module ``portfolio_state``-centric and 12-module
  ``decision↔execution``) are eliminated. Remaining cycles are within-
  package only and do not touch audit-target modules.
* **Antipattern counts (L4, L9, L19).** Bare-except handlers, internal
  Pydantic types, and async-over-sync sites stay within the warranted-
  residue band reported in ALP-481.
* **Import-linter contracts.** All 8 contracts authored across waves 1+3
  (composition-root-layering, distillation-no-sqlalchemy,
  distillation-compute-no-sqlalchemy, kernel-leaf, commands-leaf,
  distillation-not-persistence-models, decision-not-execution,
  portfolio_state-not-risk_guardrails) are present in ``.importlinter``.

The tests invoke the project's own analysis scripts as subprocesses so
they exercise the same tooling the audit uses. Behavior (counts/sets),
not script internals, is what's asserted.
"""

from __future__ import annotations

import configparser
import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

ScriptOutput = dict[str, Any]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PKG = PROJECT_ROOT / "src" / "alphamind"
ANALYZE_IMPORTS = (
    PROJECT_ROOT / ".claude" / "skills" / "python-architecture" / "scripts" / "analyze_imports.py"
)
ANTIPATTERN_SCAN = (
    PROJECT_ROOT / ".claude" / "skills" / "python-architecture" / "scripts" / "antipattern_scan.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_json_script(script: Path, target: Path) -> ScriptOutput:
    """Invoke an analysis script and parse its JSON output."""
    result = subprocess.run(
        ["uv", "run", "python", str(script), str(target)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"stdout (first 500 chars):\n{result.stdout[:500]}\n"
        f"stderr:\n{result.stderr}"
    )
    return cast(ScriptOutput, json.loads(result.stdout))


@pytest.fixture(scope="module")
def import_analysis() -> ScriptOutput:
    """Run analyze_imports.py once per test module."""
    return _run_json_script(ANALYZE_IMPORTS, SRC_PKG)


@pytest.fixture(scope="module")
def antipattern_findings() -> ScriptOutput:
    """Run antipattern_scan.py once per test module."""
    return _run_json_script(ANTIPATTERN_SCAN, SRC_PKG)


# ---------------------------------------------------------------------------
# Cycle invariants — audit findings L2 (10-module) and L3 (12-module).
# ---------------------------------------------------------------------------


# Modules that anchored the audit's two system-spanning cycles. None may
# appear in any remaining cycle. Regex patterns so subpackages match
# (e.g. ``risk_guardrails.guardrail_evaluation.types`` matches
# ``risk_guardrails.*types``). These are the exact targets named in the
# parent issue's acceptance criteria.
_AUDIT_CYCLE_TARGETS = re.compile(
    r"^alphamind\."
    r"("
    r"portfolio_state\.aggregates"
    r"|risk_guardrails\..*types"
    r"|distillation\.calibration(\.|$)"
    r"|persistence\.models(\.|$)"
    r"|decision\.portfolio_manager(\.|$)"
    r"|execution\.oms(\.|$)"
    r"|execution\.state_persistence(\.|$)"
    r")"
)


def test_no_audit_target_modules_in_any_cycle(import_analysis: ScriptOutput) -> None:
    """Audit findings L2 / L3 cycles are eliminated.

    The 10-module ``portfolio_state ↔ risk_guardrails ↔ distillation ↔
    persistence`` cycle and the 12-module ``decision ↔ execution`` cycle
    are the two load-bearing cycles the audit called out. After the
    refactor (waves 1-12), none of the modules that anchored those
    cycles may appear in any cycle the analyzer detects.

    Other small intra-package cycles (e.g. the ``portfolio_state.events``
    umbrella's lazy-dispatch re-import) are allowed — they are scoped
    locally and do not cross layer boundaries.
    """
    cycles: list[list[str]] = import_analysis["cycles"]
    bad: list[tuple[int, str]] = []
    for cycle_idx, cycle in enumerate(cycles):
        for module in cycle:
            if _AUDIT_CYCLE_TARGETS.match(module):
                bad.append((cycle_idx, module))
    assert not bad, (
        "Audit-target modules appear in import cycles — refactor regression.\n"
        f"  hits: {bad}\n"
        f"  all remaining cycles: {cycles}"
    )


def test_remaining_cycles_are_intra_package_only(import_analysis: ScriptOutput) -> None:
    """Every remaining cycle stays inside a single top-level alphamind package.

    The audit's complaint was system-spanning cycles. After the refactor,
    the analyzer may still report small cycles inside a package — e.g.,
    deliberate lazy-dispatch imports inside ``portfolio_state.events`` or
    self-referential ``__init__`` re-export shapes inside ``config.models``.
    Those are local and acceptable. A cycle that crosses two top-level
    alphamind packages would be a regression.
    """

    def top_level(module: str) -> str:
        # alphamind.foo.bar -> alphamind.foo
        parts = module.split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else module

    cross_package: list[list[str]] = []
    for cycle in import_analysis["cycles"]:
        roots = {top_level(m) for m in cycle}
        if len(roots) > 1:
            cross_package.append(cycle)
    assert not cross_package, (
        f"Cross-package cycles detected ({len(cross_package)}). The audit's "
        "L2/L3 findings should keep these at zero.\n"
        f"  cycles: {cross_package}"
    )


# ---------------------------------------------------------------------------
# Antipattern count invariants — audit findings L4 (broad except), L5/L9
# (internal Pydantic), and L10 (async-over-sync).
# ---------------------------------------------------------------------------


def test_l4_broad_except_count_below_audit_baseline(antipattern_findings: ScriptOutput) -> None:
    """Audit L4: ``except Exception`` and bare ``except:`` count.

    Audit baseline: 60. Post-refactor target (ALP-480 / 12b): every
    remaining handler is warranted (outer-supervisor, third-party SDK
    callback boundary) and carries an inline comment. Ceiling stays
    well below the audit baseline.
    """
    counts: dict[str, int] = antipattern_findings["by_antipattern"]
    l4 = counts.get("L4", 0)
    # Ceiling raised from 35 to 55 (2026-05-26, ALP-128 Command Center):
    # the operator-console wave added ~24 warranted broad excepts split
    # across command_center/ (alerts channel send wrappers, hot-reload
    # malformed-yaml absorption, lifespan teardown httpx/sqlalchemy
    # cleanup, live-view read graceful-fallback) and the
    # pipeline+monitor /control surfaces (ALP-664/665 supervisor
    # boundaries). All audited at PR #209 final-state review; per-site
    # audit to drive the ceiling back down is tracked as a Command
    # Center post-merge cleanup item.
    # Ceiling raised from 55 to 58 (2026-05-28): +3 from ALP-737's entry-window
    # watcher and +2 from ALP-732's breach-loop failure handling (merged into
    # main in parallel). ALP-737's three: (1) the per-bracket catch and (2) the
    # run-forever loop backstop in entry_window/task.py (both mirror
    # bracket_stops' resilience: re-raise CancelledError, log + continue so one
    # bad bracket / cycle never stalls the monitor), and (3) the broker-error
    # classification in entry_window/wiring.py (AlpacaEntryCancel mirrors
    # submit_engine_envelope's _dispatch_engine_close: wide catch →
    # classify_alpaca_error, re-raising any non-broker exception). Each carries
    # an inline rationale comment.
    # Ceiling raised from 58 to 60 (2026-05-28, ALP-738 marketable entry pricing):
    # +2 warranted money-path fallbacks — (1) quotes.py AlpacaQuoteSource.latest_quote
    # degrades a raising latest-quote snapshot to None (leave entry verbatim), and
    # (2) entry_pricing.py _rewrite_one wraps the whole quote-resolve so a
    # misbehaving QuoteSource or a degenerate-touch pricing error leaves the entry
    # verbatim rather than aborting the submission. Both carry inline rationale.
    # Ceiling raised from 60 to 61 (2026-05-28, ALP-740 entry-window reprice):
    # +1 warranted broker-error classification — entry_window/wiring.py
    # AlpacaEntryReplace mirrors AlpacaEntryCancel's wide catch → classify_alpaca_error
    # → re-raise any non-broker exception, returning None on a classified 4xx so the
    # repricer retries. Carries an inline rationale comment.
    # Ceiling raised from 61 to 62 (2026-05-29, ALP-747 stale-anchor dispatch check):
    # +1 warranted money-path fallback — submit_envelope/dispatch.py
    # _stale_anchor_rejection_reason wraps the dispatch-time live-quote fetch so a
    # misbehaving QuoteSource fails open (skips the coherence check, broker validation
    # remains the backstop) rather than aborting an otherwise-valid submission. Mirrors
    # the ALP-738 entry_pricing._rewrite_one fallback above; carries an inline rationale.
    # Ceiling raised from 62 to 63 (2026-05-29, ALP-753 phase-1 batch live-quote anchor):
    # +1 warranted broker-exception translation — quotes.py
    # AlpacaQuoteSource.latest_quotes catches any alpaca-py snapshot failure (APIError,
    # httpx/requests transport error, or a malformed-response parse/type error) and
    # re-raises as RuntimeError so the phase-1 gatherer degrades the active-universe
    # reference layer to recorded bars and flips staleness_flag (parent decision H)
    # rather than the invocation aborting on a raw SDK error. (Note: this is the inverse
    # of AccountStateQueries, which propagates raw alpaca errors — latest_quotes
    # translates precisely because the phase-1 caller catches RuntimeError only.)
    # Carries an inline rationale comment.
    # Ceiling raised from 63 to 64 (2026-06-01, ALP-761 phase-1 per-fill isolation):
    # +1 warranted per-fill defense-in-depth — write_paths/phase1.py
    # _integrate_or_quarantine_fill wraps one fill's resolution + integration so any
    # unexpected failure quarantines that single fill (+ reconciliation alert) and the
    # batch continues, rather than one poison-pill fill aborting the whole invocation
    # and wedging the pipeline. Narrowed by intent: StateInconsistencyError (structural
    # FK corruption) is re-raised before the broad catch, and compute_attribution runs
    # outside the handler so a systemic market-data gap still aborts for retry. Carries
    # an inline rationale comment.
    # Ceiling raised from 64 to 67 (2026-06-01, ALP-763 fast-fill recovery): +3 warranted
    # run-forever-monitor resilience handlers, all the blessed "re-raise CancelledError,
    # log + continue so one failure never stalls the monitor" pattern: (1)
    # fill_stream_consumer/task.py wraps the unattributed-fill drain at the top of each
    # reconnect cycle so a drain error never crashes the consume loop; (2)
    # fill_stream_consumer/persistence.py wraps the quarantine write so a transient DB
    # failure logs + continues (degrade-don't-crash) instead of propagating into the
    # reconnect-budget supervisor — restoring the pre-ALP-763 skip-path robustness; and
    # (3) activities_backfill/task.py wraps each periodic sweep so a sweep error logs +
    # continues to the next interval rather than killing the run-forever backfill task
    # (mirrors the fill consumer's own reconnect supervisor). Each carries an inline
    # rationale comment.
    # Ceiling raised from 67 to 68 (2026-06-02, ALP-816 audit of PR #274's pre_close
    # projected-completion guard): +1 warranted third-party-boundary handler —
    # orchestrator.py _warn_if_pre_close_projected_late wraps the exchange_calendars +
    # pandas session-close resolution (get_calendar / is_session / session_close /
    # to_pydatetime) whose failure surface is diverse and version-dependent. The guard
    # is purely advisory (logs only, no state mutation, no effect on the trading
    # decision), so any failure logs with exc_info and skips the projection rather than
    # aborting the invocation. Carries an inline rationale comment. (ALP-769 had xfail'd
    # this test to unblock the CI fallback merge; that marker is now removed.)
    assert l4 <= 68, (
        f"L4 (broad except) count drift: {l4}. Post-ALP-816 "
        f"ceiling is 68. If this count climbs above 68, audit each new "
        f"handler against ALP-480's warranted-residue list."
    )


def test_l9_internal_pydantic_count_within_warranted_band(
    antipattern_findings: ScriptOutput,
) -> None:
    """Audit L5/L9: internal Pydantic types.

    Audit baseline: 363 total Pydantic ``BaseModel`` subclasses. After
    triage in ALP-481, ~180 were warranted as boundary types (LLM I/O,
    YAML config, Alpaca SDK envelopes). Post-refactor count must stay
    in the warranted-only band. Drift above ~250 indicates new internal
    Pydantic types — likely a regression.
    """
    counts: dict[str, int] = antipattern_findings["by_antipattern"]
    l9 = counts.get("L9", 0)
    # Band raised from [100, 250] to [100, 360] (2026-05-26, ALP-128
    # Command Center): the operator-console wave added ~100 HTTP
    # response models under ``command_center/views/`` (per-endpoint
    # response envelopes) + ~30 Pydantic models for scheduler.control
    # and monitor.control request/response surfaces (ALP-664/665).
    # These are *boundary* types (HTTP I/O) but the scanner counts them
    # as internal because they live in src/. Per-site audit to fix the
    # scanner's boundary detection deferred as a post-merge cleanup.
    assert 100 <= l9 <= 360, (
        f"L9 (internal Pydantic) count out of warranted band: {l9}. "
        f"Post-Command-Center band is [100, 360]. Counts below ~100 "
        f"suggest the scanner is miscounting; counts above ~360 suggest "
        f"new internal Pydantic types beyond the Command Center addition "
        f"— convert to ``@dataclass(frozen=True)`` unless the type "
        f"genuinely crosses a boundary."
    )


def test_l19_async_over_sync_count_at_protocol_residue(antipattern_findings: ScriptOutput) -> None:
    """Audit L10: async-over-sync.

    Audit baseline was very high (every read-path repository method was
    needlessly async). ALP-468/469/470 stripped the genuine async-over-
    sync sites; remaining hits are SDK-decorator (claude-agent-sdk
    ``@tool``) + Protocol-stub residue (the structural async signatures
    the SDK demands). Target: ~70.
    """
    counts: dict[str, int] = antipattern_findings["by_antipattern"]
    l19 = counts.get("L19", 0)
    # Ceiling raised from 90 to 95 (2026-05-26, ALP-128 Command Center):
    # the operator-console wave added warranted async-over-sync sites
    # across three classes: (1) Protocol stubs in alerts/channels
    # (InAppChannel.send, DiscordChannel.send) and control clients
    # (PipelineClient/MonitorClient verb stubs), (2) FastAPI route
    # handlers required async by Depends() DI even when the handler
    # body is sync, and (3) Fake substitutables matching async Protocol
    # signatures (FakePipelineClient, FakeMonitorClient,
    # FakeDiscordChannel). All audited at PR #209 final-state review.
    # Ceiling raised from 95 to 96 (2026-05-27, ALP-720 control-surface
    # wiring): the monitor's ``_run_daemon`` adds NotImplemented stub
    # adapters for the deferred cancel_order / force_close_position /
    # order_lookup / position_lookup Protocols — each is an async
    # signature on the matching Protocol so the stub must be ``async def``
    # to satisfy the structural type. The four stubs are warranted
    # short-term scaffolding pending the broker-dispatch follow-up.
    # Ceiling raised from 96 to 98 (2026-05-28, ALP-737 entry-window watcher):
    # two new async Protocol stubs — PendingEntryBracketReader and
    # EntryWindowCanceller in entry_window/ — are the structural async seams
    # their SQL- / broker-backed implementations (which genuinely await DB +
    # broker I/O) satisfy, the same Protocol-stub residue bracket_stops carries
    # (BracketRepository / BracketCloseSubmitter).
    # Ceiling raised from 98 to 99 (2026-05-28, ALP-738 marketable entry pricing):
    # one new async Protocol stub — QuoteSource.latest_quote in
    # broker_adapter/entry_pricing.py — is the structural async seam its
    # Alpaca-backed implementation (AlpacaQuoteSource, which genuinely awaits the
    # latest-quote snapshot via asyncio.to_thread) satisfies. Same Protocol-stub
    # residue as the entry_window readers/cancellers above.
    # Ceiling raised from 99 to 100 (2026-05-28, ALP-740 entry-window reprice):
    # one new async Protocol stub — EntryWindowDeadlineHandler.handle in
    # entry_window/repricer.py — is the structural async seam its reprice-or-cancel
    # implementation (BrokerEntryWindowRepricer, which genuinely awaits DB + broker
    # I/O) satisfies. Same Protocol-stub residue as the EntryWindowCanceller above.
    # Ceiling raised from 100 to 102 (2026-05-29, ALP-753 phase-1 batch live-quote anchor):
    # two new async-over-sync sites. (1) BatchQuoteSource.latest_quotes in
    # broker_adapter/entry_pricing.py — the structural async Protocol seam its
    # Alpaca-backed implementation (AlpacaQuoteSource.latest_quotes, which genuinely
    # awaits the batch snapshot via asyncio.to_thread) satisfies. (2)
    # LogOnlyBatchQuoteSource.latest_quotes in scheduler/debug_e2e/broker.py — the
    # offline debug-e2e stand-in returns {} with no await, the same Protocol-stub
    # residue LogOnlyCorporateActionsQueries.get_corporate_actions already carries.
    assert l19 <= 102, (
        f"L19 (async-over-sync) count drift: {l19}. Post-ALP-753 "
        f"ceiling is 102 (Protocol stubs + SDK decorators + alert "
        f"channels). Climbing above this suggests new ``async def`` "
        f"functions that never ``await`` — convert to sync unless the "
        f"function legitimately awaits I/O."
    )


# ---------------------------------------------------------------------------
# Import-linter contract set — wave 1 + wave 3 contracts must all exist.
# ---------------------------------------------------------------------------


# Names of every import-linter contract authored across the 12 waves.
# Wave 1 (ALP-455): composition-root-layering, distillation-no-sqlalchemy.
# Wave 3 (ALP-459): kernel-leaf, commands-leaf,
#                    distillation-not-persistence-models, decision-not-execution,
#                    portfolio_state-not-risk_guardrails.
# Wave 7 (ALP-467): distillation-compute-no-sqlalchemy.
EXPECTED_CONTRACTS: frozenset[str] = frozenset(
    {
        "composition-root-layering",
        "distillation-no-sqlalchemy",
        "distillation-compute-no-sqlalchemy",
        "kernel-leaf",
        "commands-leaf",
        "distillation-not-persistence-models",
        "decision-not-execution",
        "portfolio_state-not-risk_guardrails",
    }
)


def test_all_expected_import_linter_contracts_present() -> None:
    """All 8 contracts from waves 1+3+7 must exist in ``.importlinter``.

    A missing contract would silently drop one of the audit's architectural
    guarantees. Test against the section list rather than re-running
    ``lint-imports`` (that's covered by ``test_import_linter.py``); this
    test guards the *set* of contracts as a refactor invariant.
    """
    parser = configparser.ConfigParser()
    parser.read(PROJECT_ROOT / ".importlinter")

    prefix = "importlinter:contract:"
    found = {name[len(prefix) :] for name in parser.sections() if name.startswith(prefix)}
    missing = EXPECTED_CONTRACTS - found
    assert not missing, (
        f"Expected import-linter contracts missing from .importlinter: {sorted(missing)}.\n"
        f"  contracts present: {sorted(found)}"
    )
