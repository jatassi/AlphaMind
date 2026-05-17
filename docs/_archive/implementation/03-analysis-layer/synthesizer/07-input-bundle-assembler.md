# 07 — Input bundle assembler

## Goal

Implement the function that composes the user-message text the synthesizer's harness (story 08) sends to the LLM: the volatility regime label, the six upstream brief texts (each with its source label and freshness), and a brief reminder of the available portfolio-state tools. Output is a single deterministic string the LLM reads as its full input.

## Reading

* `docs/design/03-analysis-layer/synthesizer.md` § Inputs — the six brief sources and their format.
* `prompts/analysis/synthesizer.md` — the system prompt this user message lands alongside; the prompt's `<inputs>` block declares the fixed reading order (`CR` then sector briefs then `QR` then `AR`) — the assembler matches that order.
* [ALP-201](https://linear.app/alphamind-jatassi/issue/ALP-201) (story 03) — `BriefBundle` data model whose fields this assembler reads.
* [ALP-203](https://linear.app/alphamind-jatassi/issue/ALP-203) (story 05a) — adapters that produce `BriefBundle` instances; the assembler consumes them.
* [ALP-205](https://linear.app/alphamind-jatassi/issue/ALP-205) (story 06a) — `retrieve_brief` MCP tool name (mentioned in the assembler's tool reminder for downstream agents, not for the synthesizer itself).
* [ALP-206](https://linear.app/alphamind-jatassi/issue/ALP-206) (story 06b) — the three portfolio-state MCP tool names listed in the assembler's tool reminder.
* [ALP-208](https://linear.app/alphamind-jatassi/issue/ALP-208) (story 08) — the harness that calls this assembler with `now_utc` for freshness computation.

## Depends on

* [ALP-201](https://linear.app/alphamind-jatassi/issue/ALP-201) (story 03) — `BriefBundle`.
* [ALP-203](https://linear.app/alphamind-jatassi/issue/ALP-203) (story 05a) — adapters used to construct the bundles upstream.
* [ALP-205](https://linear.app/alphamind-jatassi/issue/ALP-205) (story 06a) — only for the tool-name reminder text.
* [ALP-206](https://linear.app/alphamind-jatassi/issue/ALP-206) (story 06b) — only for the tool-name reminder text.

## Scope

Module path is `src/alphamind/analysis/synthesizer/input_bundle.py`. The module defines the public `assemble_input_bundle` function plus two private helpers.

#### assemble_input_bundle

Signature `assemble_input_bundle(*, regime_label: str, brief_bundles: tuple[BriefBundle, ...], portfolio_tool_names: tuple[str, ...], now_utc: datetime) -> str`. Returns a single string with this layout (where each `===` block is one section).

```
Volatility regime: <regime_label>

=== AVAILABLE TOOLS ===
Portfolio-state tools (call only when an upstream finding is portfolio-relevant):
  <portfolio_tool_names joined>

=== <source_label> (freshness: Nm ago) ===
<bundle.text>

=== <source_label> (freshness: Nm ago) ===
<bundle.text>
```

Bundles are emitted in the order the system prompt declares — `CR` first, then sector briefs in `SA-TECH` then `SA-FIN` then `SA-ENERGY` order, then `QR`, then `AR`. Bundles whose `BriefSource` is missing from the input simply do not render (not all invocations have an `AR` finding, etc.).

#### \_format_freshness helper

Signature `_format_freshness(freshness: datetime, now_utc: datetime) -> str`. Returns a human-readable "Nm ago" string. Negative deltas (clock skew) render as "0m ago" rather than negative numbers.

#### \_source_label helper

Signature `_source_label(source: BriefSource) -> str`. `BriefSource.SA_TECH` maps to `"Tech & semis sector brief"`, `BriefSource.CR` maps to `"Correlation/regime brief"`, etc. The labels match the prompt's `<inputs>` block.

The assembler does not validate the bundles — that's upstream's job. It formats whatever it receives.

#### Tests

Test module path is `tests/analysis/synthesizer/test_input_bundle.py`. Test cases listed below.

* `test_full_six_bundle_assembly` — six bundles in arbitrary input order; verify output is in canonical order with all six section headers.
* `test_partial_bundles` — only three bundles supplied (CR + SA-TECH + QR); output renders only those three sections.
* `test_regime_label_appears_first`.
* `test_portfolio_tool_names_listed`.
* `test_freshness_minutes_ago` — known timestamp delta produces expected string.
* `test_freshness_negative_clamped` — `freshness > now_utc` renders "0m ago".
* `test_source_labels_match_prompt` — the labels appear in `prompts/analysis/synthesizer.md` `<inputs>` block.

## Out of scope

* Validating the bundle texts (the upstream parsers/validators handle that).
* Constructing the bundles (story 05a).
* Calling the LLM with the assembled string (story 08).

## Acceptance criteria

- [ ] `assemble_input_bundle` returns a string with regime label first, tool reminder second, brief sections in canonical order.
- [ ] Source labels match the system prompt's `<inputs>` block descriptions.
- [ ] Freshness rendered as "Nm ago" with clock-skew clamp.
- [ ] Partial input renders only the supplied bundles.
- [ ] No `DC` prefix references anywhere.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.