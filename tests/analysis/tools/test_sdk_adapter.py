"""Tests for the analysis-layer SDK MCP-server adapter — ALP-261.

Verifies the public ``build_analysis_mcp_server`` primitive shared by the
qualitative-research and adaptive-research harnesses: dict-keyed return
shape, allowed-tool wire format, KeyError on missing tools, and namespace
separation between the two server names.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from claude_agent_sdk import ClaudeAgentOptions, McpSdkServerConfig
from claude_agent_sdk.types import (
    McpHttpServerConfig,
    McpSSEServerConfig,
    McpStdioServerConfig,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis.tools._sdk_adapter import build_analysis_mcp_server
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


def test_qualitative_server_returns_dict_keyed_servers_and_prefixed_allowed_names(
    session: Session,
) -> None:
    """``server_name='alphamind_qualitative'`` produces a dict keyed by that name
    and an allowed-tools list prefixed with ``mcp__alphamind_qualitative__``."""
    mcp_servers, allowed = build_analysis_mcp_server(
        server_name="alphamind_qualitative",
        tool_names=("news_search",),
        session=session,
    )

    assert list(mcp_servers.keys()) == ["alphamind_qualitative"]
    assert allowed == ["mcp__alphamind_qualitative__news_search"]


def test_adaptive_server_returns_namespaced_servers_and_allowed_names(
    session: Session,
) -> None:
    """Two adaptive-only tools resolve under ``alphamind_adaptive`` with
    ``mcp__alphamind_adaptive__*`` allowed names — preserving call order."""
    mcp_servers, allowed = build_analysis_mcp_server(
        server_name="alphamind_adaptive",
        tool_names=("ticker_deep_pull", "sec_lending"),
        session=session,
    )

    assert list(mcp_servers.keys()) == ["alphamind_adaptive"]
    assert allowed == [
        "mcp__alphamind_adaptive__ticker_deep_pull",
        "mcp__alphamind_adaptive__sec_lending",
    ]


def test_qualitative_and_adaptive_namespaces_do_not_overlap(session: Session) -> None:
    """Two server names produce disjoint allowed-tools lists even when both
    register the same underlying tool — the wire prefix carries the namespace."""
    _, qualitative_allowed = build_analysis_mcp_server(
        server_name="alphamind_qualitative",
        tool_names=("news_search", "earnings_commentary"),
        session=session,
    )
    _, adaptive_allowed = build_analysis_mcp_server(
        server_name="alphamind_adaptive",
        tool_names=("news_search", "earnings_commentary"),
        session=session,
    )

    assert set(qualitative_allowed).isdisjoint(adaptive_allowed)
    assert all(name.startswith("mcp__alphamind_qualitative__") for name in qualitative_allowed)
    assert all(name.startswith("mcp__alphamind_adaptive__") for name in adaptive_allowed)


def test_unknown_tool_name_raises_key_error(session: Session) -> None:
    """A tool name absent from ``TOOLS`` causes ``KeyError`` — the function
    fails loudly rather than dropping the privilege silently."""
    with pytest.raises(KeyError):
        build_analysis_mcp_server(
            server_name="alphamind_qualitative",
            tool_names=("not_a_real_tool",),
            session=session,
        )


def test_built_server_is_assignable_to_claude_agent_options(
    session: Session,
) -> None:
    """End-to-end shape check: the return value populates ``ClaudeAgentOptions``
    fields directly — no harness wrapping required.

    This is the architectural contract the qualitative-research and
    adaptive-research harnesses depend on: the dict keys ``mcp_servers``
    and the prefixed strings populate ``allowed_tools``.  Types are widened
    to the SDK's union at the boundary because ``dict`` is invariant in
    its value type.
    """
    mcp_servers, allowed = build_analysis_mcp_server(
        server_name="alphamind_adaptive",
        tool_names=("ticker_deep_pull", "macro_data"),
        session=session,
    )
    sdk_servers: dict[
        str,
        McpStdioServerConfig | McpSSEServerConfig | McpHttpServerConfig | McpSdkServerConfig,
    ] = dict(mcp_servers)

    options = ClaudeAgentOptions(
        system_prompt="test",
        allowed_tools=allowed,
        mcp_servers=sdk_servers,
    )

    assert options.allowed_tools == allowed
    assert options.mcp_servers == sdk_servers
    assert "alphamind_adaptive" in options.mcp_servers
    server_config = options.mcp_servers["alphamind_adaptive"]
    assert server_config["type"] == "sdk"
    assert server_config["name"] == "alphamind_adaptive"
