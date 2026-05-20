"""Shared Linear GraphQL helpers for the operator tooling in ``scripts/``.

The Linear MCP server cannot count issues, archive them, or return
un-truncated issue bodies — the ``scripts/*linear*`` entry points hit the
GraphQL API directly instead. This module centralises authentication, the
POST/error wrapper, connection pagination, and the handful of issue
queries those scripts share, so each entry point stays thin.

Requires ``LINEAR_API_KEY`` in the environment or the repo-root ``.env``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

import httpx
from dotenv import load_dotenv

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
FREE_TIER_CAP = 250

_PAGE_SIZE = 250
_HTTP_TIMEOUT = 30.0

# Issue fields every script needs. ``description`` is fetched separately
# (see ``ISSUE_FIELDS_WITH_BODY``) because issue bodies are large.
ISSUE_FIELDS = (
    "id identifier title url archivedAt "
    "state { name type } project { name } parent { id identifier title }"
)
ISSUE_FIELDS_WITH_BODY = ISSUE_FIELDS + " description"


class LinearError(RuntimeError):
    """A Linear API call failed — auth, transport, or a GraphQL-level error."""


def load_api_key() -> str:
    """Return ``LINEAR_API_KEY``, loading the repo-root ``.env`` first.

    Raises :class:`LinearError` if the key is absent.
    """
    load_dotenv()
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise LinearError(
            "LINEAR_API_KEY not set — add it to the repo-root .env or the environment"
        )
    return key


def graphql(
    api_key: str,
    document: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST a GraphQL document to Linear and return its ``data`` object.

    Raises :class:`LinearError` on transport failure, a non-2xx status, or
    a GraphQL-level ``errors`` array.
    """
    try:
        response = httpx.post(
            LINEAR_GRAPHQL_URL,
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={"query": document, "variables": variables or {}},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        hint = " — check LINEAR_API_KEY" if exc.response.status_code in (401, 403) else ""
        raise LinearError(f"Linear API returned HTTP {exc.response.status_code}{hint}") from exc
    except httpx.HTTPError as exc:
        raise LinearError(f"could not reach Linear: {exc}") from exc

    payload: dict[str, Any] = response.json()
    errors = payload.get("errors")
    if errors:
        messages = "; ".join(str(err.get("message", err)) for err in errors)
        raise LinearError(f"Linear GraphQL error: {messages}")
    data: dict[str, Any] = payload.get("data") or {}
    return data


def iter_issues(
    api_key: str,
    *,
    issue_filter: dict[str, Any] | None = None,
    fields: str = ISSUE_FIELDS,
    include_archived: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield every issue matching ``issue_filter`` (the whole workspace if None).

    Pages the root ``issues`` connection 250 nodes at a time. ``fields`` is
    the GraphQL selection set for each node — pass :data:`ISSUE_FIELDS_WITH_BODY`
    when the issue body is needed.
    """
    document = f"""
    query IterIssues($after: String, $filter: IssueFilter) {{
      issues(
        first: {_PAGE_SIZE}
        includeArchived: {"true" if include_archived else "false"}
        after: $after
        filter: $filter
      ) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {fields} }}
      }}
    }}
    """
    after: str | None = None
    while True:
        data = graphql(api_key, document, {"after": after, "filter": issue_filter})
        connection = data["issues"]
        yield from connection["nodes"]
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            return
        after = page_info["endCursor"]
        if after is None:
            raise LinearError("Linear reported hasNextPage but returned no endCursor")


def iter_children(
    api_key: str,
    parent_uuid: str,
    *,
    fields: str = ISSUE_FIELDS,
    include_archived: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield every direct sub-issue of the given parent UUID."""
    return iter_issues(
        api_key,
        issue_filter={"parent": {"id": {"eq": parent_uuid}}},
        fields=fields,
        include_archived=include_archived,
    )


def resolve_issue(api_key: str, identifier: str) -> dict[str, Any]:
    """Resolve a human identifier like ``ALP-123`` to its issue node.

    Raises :class:`LinearError` for a malformed identifier or no match.
    """
    match = re.fullmatch(r"\s*([A-Za-z]+)-(\d+)\s*", identifier)
    if match is None:
        raise LinearError(f"not a Linear issue identifier: {identifier!r}")
    team_key = match.group(1).upper()
    number = int(match.group(2))
    issue_filter = {"team": {"key": {"eq": team_key}}, "number": {"eq": number}}
    nodes = list(iter_issues(api_key, issue_filter=issue_filter, include_archived=True))
    if not nodes:
        raise LinearError(f"no issue found for {identifier}")
    return nodes[0]


def archive_issue(api_key: str, issue_uuid: str) -> bool:
    """Archive an issue — recoverable indefinitely, and frees a free-tier slot.

    Returns the mutation's ``success`` flag. Archiving (not trashing) is
    deliberate: archived issues stay recoverable forever via Linear's
    archive view, whereas trashed issues are purged after 30 days.
    """
    data = graphql(
        api_key,
        "mutation ArchiveIssue($id: String!) { issueArchive(id: $id) { success } }",
        {"id": issue_uuid},
    )
    return bool(data["issueArchive"]["success"])
