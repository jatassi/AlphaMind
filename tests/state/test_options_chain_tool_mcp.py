"""Tests for the ``retrieve_options_chain`` MCP tool (ALP-948).

Exercises the per-invocation SDK MCP server against an in-memory fake
:class:`OptionsChainReader` — the SQL shell has its own seeded-SQLite tests
in ``test_options_chain_read.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import alphamind.state.invocation_context  # noqa: F401  — import-order priming (see test_sql_option_price_provider)
from alphamind._kernel.ids import OccSymbol
from alphamind.state.repository.options_chain_read import (
    ChainSlice,
    ContractQuote,
    TickerOptionsContext,
    occ_symbol_for_contract,
)
from alphamind.state.repository.options_chain_tool_mcp import (
    RETRIEVE_OPTIONS_CHAIN_TOOL_NAME,
    build_retrieve_options_chain_mcp_server,
)

_NOW = datetime(2026, 6, 10, 14, 30, 0, tzinfo=UTC)


@dataclass
class _FakeReader:
    """In-memory ``OptionsChainReader`` substitute."""

    slices: dict[str, ChainSlice] = field(default_factory=dict)

    def chain_slice(self, underlying: str) -> ChainSlice | None:
        return self.slices.get(underlying)

    def latest_quote(self, occ: OccSymbol) -> ContractQuote | None:
        for chain in self.slices.values():
            for quote in chain.contracts:
                if quote.occ_symbol == occ:
                    return quote
        return None

    def options_context(self, underlying: str) -> TickerOptionsContext | None:
        return None


def _nvda_quote() -> ContractQuote:
    return ContractQuote(
        occ_symbol=occ_symbol_for_contract(
            underlying="NVDA",
            expiration=date(2026, 7, 17),
            strike=850.0,
            contract_type="call",
        ),
        underlying="NVDA",
        expiration=date(2026, 7, 17),
        strike=850.0,
        contract_type="call",
        bid=24.10,
        ask=24.90,
        implied_volatility=0.41,
        delta=0.52,
        open_interest=1834,
        snapshot_ts=_NOW - timedelta(minutes=12),
    )


def _nvda_slice(*, omitted: str | None = None) -> ChainSlice:
    return ChainSlice(
        underlying="NVDA",
        spot=842.50,
        as_of=_NOW,
        contracts=(_nvda_quote(),),
        omitted=omitted,
    )


async def _call_tool(reader: _FakeReader, underlying: str) -> str:
    mcp_servers, allowed = build_retrieve_options_chain_mcp_server(reader)
    assert allowed == [RETRIEVE_OPTIONS_CHAIN_TOOL_NAME]
    server: Any = mcp_servers["alphamind_options_chain"]["instance"]

    from mcp.types import CallToolRequest, CallToolRequestParams

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="retrieve_options_chain", arguments={"underlying": underlying}
        ),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(request)
    assert not result.root.isError
    text = result.root.content[0].text
    assert isinstance(text, str)
    return text


async def test_tool_renders_header_contract_lines_and_omitted_note() -> None:
    reader = _FakeReader(slices={"NVDA": _nvda_slice(omitted="omitted: 3 below the floor")})

    text = await _call_tool(reader, "NVDA")

    lines = text.splitlines()
    assert lines[0] == f"NVDA options chain — spot 842.50, as of {_NOW.isoformat()}"
    contract_line = lines[1]
    assert "NVDA260717C00850000" in contract_line
    assert "2026-07-17 850C" in contract_line
    assert "bid/ask 24.10/24.90" in contract_line
    assert "IV 0.41" in contract_line
    assert "delta +0.52" in contract_line
    assert "OI 1834" in contract_line
    assert "age 12m" in contract_line
    assert lines[-1] == "omitted: 3 below the floor"


async def test_tool_uppercases_the_requested_underlying() -> None:
    reader = _FakeReader(slices={"NVDA": _nvda_slice()})

    text = await _call_tool(reader, "  nvda ")

    assert text.startswith("NVDA options chain")


async def test_tool_returns_explicit_no_chain_data_message_not_an_error() -> None:
    text = await _call_tool(_FakeReader(), "TSLA")

    assert "No usable options chain data for TSLA" in text


async def test_input_schema_has_exactly_one_required_string_property() -> None:
    from alphamind.state.repository.options_chain_tool_mcp import (
        _RETRIEVE_OPTIONS_CHAIN_INPUT_SCHEMA,
    )

    assert _RETRIEVE_OPTIONS_CHAIN_INPUT_SCHEMA["required"] == ["underlying"]
    assert set(_RETRIEVE_OPTIONS_CHAIN_INPUT_SCHEMA["properties"]) == {"underlying"}
    assert _RETRIEVE_OPTIONS_CHAIN_INPUT_SCHEMA["properties"]["underlying"]["type"] == "string"
