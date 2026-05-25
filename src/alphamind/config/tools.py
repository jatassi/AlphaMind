"""Registered-tool registry for cross-reference validation (story 06a).

The set enumerates every tool name an agent's ``tools`` allowlist (and the
adaptive researcher's ``tool_caps`` map) may reference. Cross-reference
validation rejects any agent config that names a tool absent from this set.

Sources transcribed from the analysis-layer specs:

* ``prompts/decision/analyst.md`` — ``validate_guardrail``, ``retrieve_brief``
* ``prompts/decision/strategist.md`` — ``validate_guardrail``,
  ``validate_guardrail_batch``, ``retrieve_brief``
* ``prompts/decision/pm.md`` — ``validate_guardrail``,
  ``validate_guardrail_batch``, ``retrieve_brief``,
  ``get_thesis_components``, ``submit_envelope``
* ``docs/design/03-analysis-layer/adaptive-research.md § Tool inventory`` —
  ``news_search``, ``ticker_deep_pull``, ``social_sentiment``,
  ``prediction_markets``, ``options_flow``, ``sec_lending``, ``short_interest``,
  ``earnings_calendar``, ``macro_data``
* ``docs/design/03-analysis-layer/qualitative-research.md § On-demand tools`` —
  ``earnings_commentary`` (new, qualitative-researcher-only)

The set is a module-level constant; runtime tool registration and dispatch live
in the tool harness, not here. Future stories may extend the set.
"""

REGISTERED_TOOLS: frozenset[str] = frozenset(
    {
        # Decision-layer tools (analyst, strategist, pm).
        "retrieve_brief",
        "validate_guardrail",
        # Strategist / PM batch sibling of validate_guardrail (ALP-625).
        "validate_guardrail_batch",
        # PM-only tools.
        "get_thesis_components",
        "submit_envelope",
        # Adaptive researcher tools.
        "news_search",
        "ticker_deep_pull",
        "social_sentiment",
        "prediction_markets",
        "options_flow",
        "sec_lending",
        "short_interest",
        "earnings_calendar",
        "macro_data",
        # Qualitative researcher tools (shares three with adaptive; one new).
        "earnings_commentary",
    }
)
