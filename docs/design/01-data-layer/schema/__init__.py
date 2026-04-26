"""AlphaMind Data Layer Schema — Target Entity Definitions.

This package defines the canonical schema for all data consumed by AlphaMind's
6-layer pipeline. Each module corresponds to a domain from the external data
specification (quantitative categories Q1–Q12, qualitative categories Qual 1–6).

Phase 1: Entity stubs (class + docstring, no fields) ✓ Complete
Phase 2: Field-level definitions with types, constraints, and documentation ✓ Complete

Entity naming convention:
    - Class names match the "Target Entity / Object" column in source-to-target-mapping.xlsx
    - Spec IDs (e.g., Q1:1a) are referenced in docstrings
    - Each entity maps 1:1 to a sub-category in the design spec

Package structure:
    Quantitative (10 modules, 65 entities):
        price_volume        Q1:1a–1g    7 entities
        order_flow          Q2:2a–2f    6 entities
        derivatives         Q3:3a–3h    8 entities
        short_selling       Q4:4a–4e    5 entities
        fundamentals        Q5:5a–5g    7 entities
        macro               Q6:6a–6g    7 entities
        cross_asset         Q7:7a–7g    7 entities
        commodities         Q8:8a–8f    6 entities
        volatility          Q11:11a–11f 6 entities
        corporate_actions   Q12:12a–12f 6 entities

    Qualitative (5 modules, 21 entities):
        news_sentiment      Qual 1:1a–1b + Qual 2:2a–2c   5 entities
        prediction_markets  Qual 3:3a–3c                   3 entities
        earnings_commentary Qual 4:4a–4e                   5 entities
        regulatory          Qual 5:5a–5e                   5 entities
        sector_catalysts    Qual 6:6a–6c                   3 entities

    Reference Data (1 module, 2 entities):
        reference           REF:UNIVERSE, REF:SECTOR       2 entities

    Cross-cutting (in fundamentals module):
        ValuationMultiples  Q5:VAL                         1 entity

    Infrastructure:
        _common             Shared types, enums, and base definitions
        _registry           Spec ID → entity class mapping
"""

# Common types (imported by all domain modules)
from ._common import *

# Quantitative domains
from .price_volume import *
from .order_flow import *
from .derivatives import *
from .short_selling import *
from .fundamentals import *
from .macro import *
from .cross_asset import *
from .commodities import *
from .volatility import *
from .corporate_actions import *

# Reference data
from .reference import *

# Qualitative domains
from .news_sentiment import *
from .prediction_markets import *
from .earnings_commentary import *
from .regulatory import *
from .sector_catalysts import *

# Registry
from ._registry import ENTITY_REGISTRY
