"""Entity Registry — maps spec IDs to (module, class_name) tuples.

This registry connects the schema back to the source-to-target-mapping
spreadsheet (Sheet 1 "Domain-Entity Mapping") and enables programmatic
verification of complete coverage.

Usage:
    from schema._registry import ENTITY_REGISTRY, get_entity_class

    # Look up by spec ID
    module, cls_name = ENTITY_REGISTRY["Q1:1a"]
    # -> ("price_volume", "MultiTimeframePriceBar")

    # Get the actual class
    cls = get_entity_class("Q1:1a")
    # -> <class 'schema.price_volume.MultiTimeframePriceBar'>
"""

import importlib

# Spec ID -> (module_name, class_name)
# Module names are relative to the schema package.
ENTITY_REGISTRY: dict[str, tuple[str, str]] = {
    # ── Q1: Price and Volume ──
    "Q1:1a": ("price_volume", "MultiTimeframePriceBar"),
    "Q1:1b": ("price_volume", "VolumeProfile"),
    "Q1:1c": ("price_volume", "TechnicalIndicator"),
    "Q1:1d": ("price_volume", "GapAnalysis"),
    "Q1:1e": ("price_volume", "RelativePerformance"),
    "Q1:1f": ("price_volume", "HistoricalContext"),
    "Q1:1g": ("price_volume", "ExtendedHoursBar"),
    # ── Q2: Order Flow ──
    "Q2:2a": ("order_flow", "TradeFlowAggregate"),
    "Q2:2b": ("order_flow", "LiquiditySnapshot"),
    "Q2:2c": ("order_flow", "InstitutionalFlow"),
    "Q2:2d": ("order_flow", "AuctionData"),
    "Q2:2e": ("order_flow", "IntradayFlowPattern"),
    "Q2:2f": ("order_flow", "ExtendedHoursFlow"),
    # ── Q3: Derivatives ──
    "Q3:3a": ("derivatives", "ImpliedVolSurface"),
    "Q3:3b": ("derivatives", "UnusualOptionsActivity"),
    "Q3:3c": ("derivatives", "OpenInterestLandscape"),
    "Q3:3d": ("derivatives", "DealerExposure"),
    "Q3:3e": ("derivatives", "EarningsImpliedMove"),
    "Q3:3f": ("derivatives", "PutCallDynamics"),
    "Q3:3g": ("derivatives", "CrossTickerOptionsSignal"),
    "Q3:3h": ("derivatives", "SectorETFOptionsFlow"),
    # ── Q4: Short Selling ──
    "Q4:4a": ("short_selling", "ShortInterestSnapshot"),
    "Q4:4b": ("short_selling", "BorrowCost"),
    "Q4:4c": ("short_selling", "ShortVolumeDaily"),
    "Q4:4d": ("short_selling", "LendingMarketStructure"),
    "Q4:4e": ("short_selling", "SqueezeComposite"),
    # ── Q5: Fundamentals ──
    "Q5:5a": ("fundamentals", "EarningsEstimateDynamics"),
    "Q5:5b": ("fundamentals", "EarningsCalendar"),
    "Q5:5c": ("fundamentals", "RevenueGrowthTrajectory"),
    "Q5:5d": ("fundamentals", "MarginProfitabilityShift"),
    "Q5:5e": ("fundamentals", "EarningsEstimates"),
    "Q5:5f": ("fundamentals", "AnalystRatings"),
    "Q5:5g": ("fundamentals", "InsiderInstitutionalOwnership"),
    # ── Q6: Macro/Rates ──
    "Q6:6a": ("macro", "YieldCurve"),
    "Q6:6b": ("macro", "FedPolicyExpectations"),
    "Q6:6c": ("macro", "InflationMetrics"),
    "Q6:6d": ("macro", "GrowthIndicators"),
    "Q6:6e": ("macro", "CreditConditions"),
    "Q6:6f": ("macro", "CurrencyAndDollar"),
    "Q6:6g": ("macro", "MacroEventCalendar"),
    # ── Q7: Cross-Asset ──
    "Q7:7a": ("cross_asset", "IntraSectorCorrelation"),
    "Q7:7b": ("cross_asset", "CrossSectorRotation"),
    "Q7:7c": ("cross_asset", "IndexConstituentBehavior"),
    "Q7:7d": ("cross_asset", "IntermarketRegimeSignal"),
    "Q7:7e": ("cross_asset", "ImpliedRealizedCorrelation"),
    "Q7:7f": ("cross_asset", "LeadLagRelationship"),
    "Q7:7g": ("cross_asset", "CorrelationRegimeChange"),
    # ── Q8: Commodities ──
    "Q8:8a": ("commodities", "CrudeOil"),
    "Q8:8b": ("commodities", "NaturalGas"),
    "Q8:8c": ("commodities", "IndustrialMetals"),
    "Q8:8d": ("commodities", "PreciousMetals"),
    "Q8:8e": ("commodities", "CrackSpreads"),
    "Q8:8f": ("commodities", "CommodityPositioning"),
    # ── Q11: Volatility ──
    "Q11:11a": ("volatility", "VIXDynamics"),
    "Q11:11b": ("volatility", "VIXTermStructure"),
    "Q11:11c": ("volatility", "VVIX"),
    "Q11:11d": ("volatility", "RealizedImpliedVolGap"),
    "Q11:11e": ("volatility", "SKEWIndex"),
    "Q11:11f": ("volatility", "VolRegimeClassification"),
    # ── Q12: Corporate Actions ──
    "Q12:12a": ("corporate_actions", "IndexRebalance"),
    "Q12:12b": ("corporate_actions", "BuybackProgram"),
    "Q12:12c": ("corporate_actions", "RegulatoryFiling"),
    "Q12:12d": ("corporate_actions", "InvestorEventCalendar"),
    "Q12:12e": ("corporate_actions", "LockupSecondaryCalendar"),
    "Q12:12f": ("corporate_actions", "ETFFlowImpact"),
    # ── Qual 1: News ──
    "Qual1:1a": ("news_sentiment", "BreakingHeadline"),
    "Qual1:1b": ("news_sentiment", "LongFormAnalysis"),
    # ── Qual 2: Sentiment ──
    "Qual2:2a": ("news_sentiment", "SocialSentimentScore"),
    "Qual2:2b": ("news_sentiment", "AlternativeSentimentProxy"),
    "Qual2:2c": ("news_sentiment", "OptionsFlowNarrative"),
    # ── Qual 3: Prediction Markets ──
    "Qual3:3a": ("prediction_markets", "MonetaryPolicyOutcome"),
    "Qual3:3b": ("prediction_markets", "RegulatoryPoliticalOutcome"),
    "Qual3:3c": ("prediction_markets", "GeopoliticalOutcome"),
    # ── Qual 4: Earnings Commentary ──
    "Qual4:4a": ("earnings_commentary", "ManagementToneAnalysis"),  # 4a-4b in spreadsheet
    "Qual4:4b": ("earnings_commentary", "AnalystQADynamics"),  # but separate entities in schema
    "Qual4:4c": ("earnings_commentary", "CrossCompanyEarningsIntel"),
    "Qual4:4d": ("earnings_commentary", "CrossCompanyEarningsIntel"),  # 4c-4d combined
    "Qual4:4e": ("earnings_commentary", "ConferencePresentation"),
    # ── Qual 5: Regulatory ──
    "Qual5:5a": ("regulatory", "FedCommunication"),
    "Qual5:5b": ("regulatory", "RegulatoryAction"),
    "Qual5:5c": ("regulatory", "ExecutiveAction"),
    "Qual5:5d": ("regulatory", "GeopoliticalEvent"),
    "Qual5:5e": ("regulatory", "PolicyEventCalendar"),
    # ── Qual 6: Sector Catalysts ──
    "Qual6:6a": ("sector_catalysts", "TechSemiCatalyst"),
    "Qual6:6b": ("sector_catalysts", "EnergyCatalyst"),
    "Qual6:6c": ("sector_catalysts", "FinancialsCatalyst"),
    # ── Reference Data (no spec ID — structural) ──
    "REF:UNIVERSE": ("reference", "AssetUniverse"),
    "REF:SECTOR": ("reference", "SectorClassification"),
    # ── Cross-cutting (valuation — derived from Q5 financials + prices) ──
    "Q5:VAL": ("fundamentals", "ValuationMultiples"),
}


def get_entity_class(spec_id: str) -> type:
    """Dynamically import and return the entity class for a given spec ID."""
    if spec_id not in ENTITY_REGISTRY:
        raise KeyError(f"Unknown spec ID: {spec_id}")
    module_name, class_name = ENTITY_REGISTRY[spec_id]
    module = importlib.import_module(f".{module_name}", package="schema")
    return getattr(module, class_name)


# ── Summary statistics ──
DOMAIN_COUNTS: dict[str, int] = {}
for module, _ in ENTITY_REGISTRY.values():
    DOMAIN_COUNTS[module] = DOMAIN_COUNTS.get(module, 0) + 1

TOTAL_ENTITIES = len(ENTITY_REGISTRY)
TOTAL_DOMAINS = len(DOMAIN_COUNTS)
