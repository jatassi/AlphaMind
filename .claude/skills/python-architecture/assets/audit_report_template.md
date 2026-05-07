<!--
This is a scaffold for audit reports. Use it as a starting point; deviate
where the actual findings demand a different shape. The order of sections
below is the recommended reading order: scope first so the reader knows
what was looked at, then the load-bearing findings (the ones that matter
most), then high-yield findings (cheap fixes with real payoff), then
worth-knowing observations, then a punch list.

Sections marked OPTIONAL can be cut if not relevant.

Inline comments throughout (the <!-- ... -->) are guidance for the agent
filling in the template. Strip them before delivering.
-->

# Audit — `<package or scope>`

**Date:** `<YYYY-MM-DD>`
**Scope:** `<which package(s), how deep, what concerns>`
**Constraints noted:** `<frameworks/decisions the user said not to re-litigate>`

---

## 1. Calibration

<!--
2-4 paragraphs of plain prose. Tell the user:
  - What you read (pyproject.toml, README, docs/architecture/, top-level layout)
  - The headline shape: monorepo? src/ layout? framework? Python version?
  - What the code claims to be vs. what it actually is — any obvious gap

Keep it short. The reader knows their codebase; this is to confirm you do too.
-->

---

## 2. Scripts run

<!--
List of structural scripts run, with one-line summary of what each found.
Don't dump JSON — synthesise. The detailed JSON is what informs the rest of
the report; it doesn't need to be reproduced verbatim.

Example shape:
  - package_overview: 373 modules; 27 god-module candidates (top: harness.py at 550 lines); 222 modules without __all__.
  - analyze_imports: 1137 edges; 4 cycles (one cross-feature: portfolio_state ↔ risk_guardrails ↔ distillation).
  - antipattern_scan: 225 findings, dominated by L12 (88, float-for-money), L19 (55, async-no-await), L25 (43, print in non-CLI).
-->

---

## 3. Findings — load-bearing

<!--
The findings that justify deliberate work. Each one violates a P1-P8
principle or surfaces a structural pathology. Use the per-finding
template below. Keep this list short (typically 3-7 findings); reserve
"load-bearing" for things that actually are.

Sort by impact, not severity. The first finding should be the one the
user benefits most from acting on.
-->

### 3.1 `<one-sentence finding>`

**Where.** `<file paths, or "package-wide">`

**Why it matters.** `<one or two sentences citing the principle (P1-P8) or
reference thesis (e.g., references/data-and-types.md §D2). The user should
be able to disagree with the principle and see the downstream change.>`

**Fix.** `<one-line concrete change. If the change is non-trivial, point to
the reference section that explains how.>`

<!-- Repeat per load-bearing finding. -->

---

## 4. Findings — high-yield

<!--
Local antipatterns that are easy to fix and pay off immediately.
Examples: mutable dataclasses, naive datetimes, missing timeouts, free-text
logs, mutable default args. Each is small individually; in aggregate they
matter, especially for the patterns that establish habits across the
codebase.

Group by antipattern ID where possible. Show counts. Cite a couple of
examples; don't list all instances unless the user asked for an exhaustive
list.
-->

### `<antipattern-id>: <one-line>` (`<count>` instances)

`<one-line summary of where the instances cluster. Two or three example
file:line references. The fix.>`

<!-- Repeat per antipattern. -->

---

## 5. Findings — worth knowing OPTIONAL

<!--
Observations that aren't problems today but will become problems if the
system grows in a particular direction. These are forward-looking; the
user may decide they're not worth acting on yet.

Examples:
  - "No import-linter configuration; layer rules are aspirational. Worth
    adding when the codebase crosses ~50 modules."
  - "Test coverage of the analysis layer is 40%; the layer's own logic is
    relatively simple but the integration with the LLM SDK isn't covered."
  - "No structured logging library; current logs are free-text via
    logging.info. Worth migrating to structlog before the system is
    deployed beyond development."

Keep this section short. If there's nothing forward-looking worth saying,
omit it.
-->

---

## 6. What looked good OPTIONAL

<!--
A short paragraph noting things the codebase does well, anchored in
specifics. This section earns credibility — the user knows you read the
code carefully — but only if it's specific. "Good use of type hints" is
filler; "Type hints are present and consistent across the entire
distillation layer; mypy strict passes there" is substance.

Skip this section if there's nothing specific to say.
-->

---

## 7. Punch list

<!--
Suggested ordering — what to tackle first, balancing load-bearing-ness
against ease of fix. The user wants a list they can start working from
tomorrow.

Format suggestion:

  1. [quick win] Fix L11 naive-datetime instances (12 cases). 1 hour.
  2. [quick win] Add timeouts to httpx calls in collector/ (8 cases). 1 hour.
  3. [structural] Resolve the cycle between portfolio_state and risk_guardrails. 1-2 days.
  4. [structural] Convert internal-domain Pydantic models to frozen dataclasses, starting with portfolio_state. 1 week.
  5. [forward-looking] Add import-linter contracts before the next layer is added.

The user may not act on all of these; that's fine. The list lets them pick.
-->

---

## 8. Open questions for the user OPTIONAL

<!--
Things you flagged but that depend on context you don't have. Example:
"The pipeline orchestrator catches Exception broadly at the top level. This
is correct for the supervisor pattern (P7) — confirm that's the intent
rather than something to tighten."

Use this sparingly. The audit's job is to give an opinionated answer, not
to bounce questions back. But where context genuinely affects the
recommendation, naming the question is honest.
-->

---

<!--
Pre-deliverable check (from SKILL.md §7):

  - [ ] Every finding cites a principle (P1-P8) or specific reference thesis.
  - [ ] Every "don't" has a paired concrete "do this instead".
  - [ ] Severity / priority is explicit; no inference required.
  - [ ] No generic Python advice; every finding is specific to this codebase.
  - [ ] No findings drawn from memory; each anchored in a thesis.
  - [ ] If a dimension found nothing, that's stated in one sentence — not padded.
  - [ ] Final punch list is the action item the user can start on.

Strip all <!-- ... --> comments before delivering.
-->
