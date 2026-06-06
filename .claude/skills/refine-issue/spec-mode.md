# Spec mode: sharpen an underspecified issue into an implementation-ready spec

When `refine-issue` triages an issue as **non-bug work** — a feature story, a refactor, a
chore — this file governs. The issue names something to build or change but leaves the
implementer guessing: vague scope, umbrella or missing acceptance criteria, no reading list,
design decisions left open, identifiers that may be stale. Close every gap so a fresh agent
can implement it cold.

The generic bar, the common Reading / Scope / Acceptance criteria / Verification sections, the
writing rules, and the Linear render mechanics live in `docs/agents/implementation-ready-issue.md`.
This file adds the spec-specific stance, the three issue shapes, and the spec-specific body
sections.

## The core principle

> Drive past the issue's vague framing to a complete, correctly-scoped specification
> **grounded in the as-built code** — every interface pinned to a concrete named contract, and
> every decision the code and design docs can settle already settled.

A bug investigation refutes the reported *cause*; a spec refinement challenges the reported
*approach*. Both treat the issue body as a hypothesis and confirm against a primary source —
for spec mode, the primary source is the code as it exists today, not the issue's prose about
it.

## The specifying stance

### 1. The framing is a hypothesis — challenge the change before you specify it

Before pinning down *how*, ask whether *this* is the right change at all: right **change**
(does it solve the actual need, or a symptom of it?), right **place** (does it belong in this
layer / module, or does the seam already live elsewhere?), right **size** (one issue, or
secretly a feature → hand to `/draft-user-stories`; or trivially small → fold into a
neighbor?). A beautifully-specified wrong change is worse than a rough right one. When the
issue's proposed approach is wrong, the rewrite says so plainly and specifies the right one.

### 2. Reuse before construction — the best refinement often shrinks the scope

Look hard for an existing mechanism that already does most of this before specifying anything
new. The highest-value outcome of a refinement is frequently "extend the existing `X` / call
the existing `Y`" rather than "build a new thing" — less code, fewer tests, no duplicated
primitive. Grep the codebase for the capability by *behavior*, not just by name. If you find
yourself specifying a new typed record, loader, or validator, first confirm one doesn't already
exist upstream that you should import or extend.

### 3. Verify ground truth against primary sources — the code, and the production data

The issue's description of how things work is a *claim*, confirmed against a primary source —
never against the issue's own narrative. Two primary sources matter for a spec:

**The as-built code.** Current-behavior prose is routinely stale: a sibling feature shipped the
record ahead of its owning issue, a config key was renamed, a function grew a parameter. Confirm
every load-bearing "currently X" claim by reading the code at `file:line`. **Pin exact
identifiers by reading them**, not from memory or the issue text: the real type name, the real
field names, the real config key, the real signature. A spec that names a symbol that doesn't
exist (or has a different shape) sends the implementer down a false trail.

**The production data**, where the design rests on an assumption about real state. Specs
routinely assume a data invariant — "every position carries a thesis", "this column is never
null", "calibration states are roughly evenly distributed", "we hold ~60–80 symbols". Those are
ground-truth claims, as verifiable as a bug's symptom: query the production DB (and live Alpaca /
archived invocation artifacts) per `production-evidence.md` to confirm them *before* baking them
into the design. Use prod evidence to confirm a data invariant the design depends on; to read
the real cardinality / distribution / null-rate that should drive a threshold or a branch; and to
verify the degenerate case (empty, null, single-row) actually occurs and must be handled. A
feature whose branches assume a distribution prod doesn't have, or a filter on a column that's
half-null in practice, is a spec that surprises the implementer — or ships and misbehaves.
Read-only, checkpoint-snapshot caveats apply (see `production-evidence.md`).

### 4. Pin every interface to a concrete, named contract

Leave nothing about an interface to the implementer when the choice ripples beyond this issue.
Name the concrete `module.py`, the concrete type/union and its fields, the concrete function
name and signature (parameter types, return shape, edge-case semantics), the concrete config
key, the concrete error name. Quote the design doc where one exists. **Never invent a name
without precedent** — every typed value object either mirrors an existing upstream record or is
named by a design doc; if you're coining one with no precedent, stop and surface it.

### 5. Place the work in the right layer and shape

A refined issue respects the architecture, it doesn't fight it. Confirm the change honors the
import direction enforced by `.importlinter` (downward-only); keep pure domain logic in the
functional core and I/O in the imperative shell; identify the testing seam (which collaborators
get Protocols + in-process fakes). When the issue scaffolds a new module/package or restructures
an existing one, invoke `Skill("python-architecture")` — **design mode** when building something
new, **audit mode** when refactoring — and anchor the Scope to its concrete package layout,
named records, and seam. (Skill invocation runs in your thread; no Agent dispatch.)

### 6. Make scope a crisp boundary

State the in-scope deliverables and the files each touches; hold one writer per file. Name what's
**out of scope** only where a reader would plausibly assume it's in — don't enumerate
prohibitions the positive scope already excludes. For a refactor, the scope *is* the blast
radius: name every call site / consumer the change touches, because that's what an implementer
will otherwise discover the hard way.

### 7. Design the verification, not just a list of criteria

Acceptance criteria are atomic per the standard. Go further and specify *how* each is verified —
the tests the implementer will write, sociable with fakes only at the sanctioned boundaries
(CLAUDE.md "Testing"). For any threshold, point the criterion at its config source rather than
baking in a number.

### 8. Settle every decision the code and docs can settle; surface only genuine forks

Resolve from the code + design docs everything the drafting-time-vs-dispatch-time cut calls
drafting-time: config values, encoding choices (named constant vs yaml-loaded), naming, scope
boundaries, default behaviors, stub strategies. Surface to the operator only a **genuine** design
fork the evidence can't settle — a real trade-off (a narrow targeted change vs a broader
structural one; whether an adjacent concern belongs in scope). A question that needs running code
to answer (a third-party API's exact shape, an algorithm case found only in implementation) stays
as a named **surfacing condition**, not a fork left open in the spec.

### 9. A reading list that needs no rediscovery

Assemble the `file:line` reading list a fresh agent follows to implement without re-deriving the
surface area: design docs first, then config models, then upstream/downstream code, then any
sibling issue whose contract this aligns with. If this issue shares vocabulary with a sibling (a
type, a config key), use that exact name and point the reading list at where it's defined.

## Three issue shapes — what "ready" means for each

Spec mode covers three shapes; the bar is the same but the weight shifts:

- **Feature story** (new capability). The work is *design*: pin the new types, signatures, and
  the test seam (Stances 4–5; `python-architecture` design mode), and validate any data
  assumption the feature's logic rests on against production (Stance 3 — distributions,
  invariants, and degenerate cases that should drive a branch or threshold). Acceptance criteria
  assert new behaviors of named functions. The reading list leans on design docs + the upstream
  contracts it consumes.
- **Refactor** (restructure existing code, behavior-preserving). The work is *blast-radius
  mapping + invariant statement*: name every call site the change touches (Stance 6), state the
  behavior-preserving invariant, and let the **existing** tests be the primary regression guard
  (`python-architecture` audit mode; the refactor step consolidates tests too). Acceptance
  criteria are mostly "existing suite green + new structural invariant holds". Encode that
  structural invariant as an `.importlinter` contract or a ruff rule — **never** as a
  drifting-count "audit-baseline ceiling" test. The reading list is the current code and its
  consumers.
- **Chore / tooling / config** (a knob, a migration, a script, a doc). The work is *concrete
  edits*: the exact file, the exact key/value, the exact command. Watch the AlphaMind traps that
  pass lint green but break prod: a new table needs an Alembic migration **plus** a migration
  test; a new field on a `config/models/*` model re-pins the resolved-config snapshot
  (`tests/config/test_snapshot.py`). Acceptance criteria are file-exists / value-present /
  command-succeeds, verified by inspection where no programmatic test fits.

If you can't tell which shape it is, that's usually a sign the issue conflates two — split-or-
surface per the too-big screen.

## Spec-mode body sections

These wrap the common sections from the standard. Order:

```markdown
## Goal
<2–4 sentences: the precise shape of "done" — the concrete deliverable, the typed
 inputs/outputs by name, which downstream consumer depends on it. Not a restatement
 of the title; the shape of done.>

## Reading
<the file:line reading list — see the standard>

## Depends on
<sub-issue ids this is gated by, each with a one-clause reason; omit if none>

## Scope
<concrete named deliverables; one writer per file — see the standard>

### Out of scope
<only adjacent things a reader would assume are in; name the owner>

## Acceptance criteria
<atomic, testable, structural-not-numeric — see the standard>

## Verification
<the tests to write (sociable, sanctioned-boundary fakes only), the lint gate, and the
 authoritative CI full-suite gate on Windows — see the standard>
```

The Reading / Scope / Acceptance criteria / Verification mechanics are the standard's; this just
names where the spec-specific Goal / Depends on / Out of scope sit.
