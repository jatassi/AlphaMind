"""Venue configuration runtime — settlement / sessions / margin / fees (ALP-121).

This package owns the in-code venue-state surface the OMS consumes for
accounting per ``docs/design/05-execution-layer/venue-configuration.md``:
settlement-day rules per instrument class, PDT / Reg T margin tiers,
regulatory fee rate tables, market-calendar caching, and account-derived
state surfacing.

Story 01 (ALP-378) ships only the package shell — populated submodules
arrive in subsequent stories per the parent-issue dependency graph
(`ALP-121 <https://linear.app/alphamind-jatassi/issue/ALP-121>`_):

* Story 02g — ``constants.py`` (settlement days, PDT threshold, Reg T tiers,
  margin-interest tiers, regulatory fee rate table).
* Story 03a — ``calendar_cache.py`` (market-calendar fetch + cache).
* Story 03b — ``account_state.py`` (account-derived venue-state surfacer).
* Story 04a — ``settlement.py`` (T+1 settlement-date calculator).
"""
