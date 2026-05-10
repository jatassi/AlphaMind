"""v1beta1 Corporate Actions Market Data API wrapper (ALP-410).

Wraps :class:`alpaca.data.historical.corporate_actions.CorporateActionsClient`
with the same typed-tuple + awaitable surface every other broker-adapter query
exposes, so the corporate-actions package depends on a single thin local type
rather than alpaca-py's ``CorporateActionsSet`` (dict-of-lists keyed on
snake-case action-type-group names).

The wrapper:

* Flattens ``CorporateActionsSet.data`` into a single ascending-by-iteration
  ``tuple[CorporateAction, ...]`` so the fetcher iterates once.
* Defaults the ``types`` filter to the action-type members AlphaMind handles
  natively (``_DEFAULT_TYPES``); the four out-of-scope members
  (``UNIT_SPLIT``, ``REDEMPTION``, ``WORTHLESS_REMOVAL``, ``RIGHTS_DISTRIBUTION``)
  are excluded so the API does not return events the fetcher would drop
  anyway.

The factory that mints the underlying ``CorporateActionsClient`` lives in
``client_factory.py`` alongside ``build_trading_client``.
"""

from __future__ import annotations

from datetime import date

from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.models.corporate_actions import CorporateAction, CorporateActionsSet
from alpaca.data.requests import CorporateActionsRequest

# In-scope v1beta1 action types. The four omitted members
# (UNIT_SPLIT / REDEMPTION / WORTHLESS_REMOVAL / RIGHTS_DISTRIBUTION) do not
# map onto a ``CorporateActionType`` AlphaMind handles natively; they're
# filtered server-side by omitting them here, and dropped client-side as
# defense in depth if the API returns them anyway.
_DEFAULT_TYPES: tuple[CorporateActionsType, ...] = (
    CorporateActionsType.FORWARD_SPLIT,
    CorporateActionsType.REVERSE_SPLIT,
    CorporateActionsType.STOCK_DIVIDEND,
    CorporateActionsType.CASH_DIVIDEND,
    CorporateActionsType.SPIN_OFF,
    CorporateActionsType.CASH_MERGER,
    CorporateActionsType.STOCK_MERGER,
    CorporateActionsType.STOCK_AND_CASH_MERGER,
    CorporateActionsType.NAME_CHANGE,
)


class CorporateActionsQueries:
    """Async-flavored facade over ``CorporateActionsClient.get_corporate_actions``.

    All callers in the corporate-actions package consume this surface; the
    underlying SDK call is sync (httpx-based) but the wrapper exposes
    ``async def`` so it composes with the surrounding ``InvocationContext`` /
    ``InvocationHandle`` substrate.
    """

    def __init__(self, client: CorporateActionsClient) -> None:
        if not isinstance(client, CorporateActionsClient):
            msg = f"client must be a CorporateActionsClient instance, got {type(client).__name__}"
            raise TypeError(msg)
        self._client = client

    async def get_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...] | None = None,
        start: date,
        end: date,
        types: tuple[CorporateActionsType, ...] = _DEFAULT_TYPES,
    ) -> tuple[CorporateAction, ...]:
        """Fetch typed CA events, flattening per-type groups into one tuple.

        Args:
            symbols: Tickers the fetcher tracks locally.  When supplied the
                API filters results to these symbols; when ``None`` the API
                returns events for every symbol the account holds in the
                window.
            start: Inclusive start date of the lookup window.
            end: Inclusive end date of the lookup window.
            types: Action-type members to fetch.  Defaults to
                ``_DEFAULT_TYPES`` (the nine members AlphaMind handles
                natively).
        """
        request = CorporateActionsRequest(
            symbols=list(symbols) if symbols is not None else None,
            start=start,
            end=end,
            types=list(types),
        )
        result = self._client.get_corporate_actions(request)
        if not isinstance(result, CorporateActionsSet):
            msg = (
                "get_corporate_actions returned unexpected raw-data response; "
                "is the client configured with raw_data=True?"
            )
            raise TypeError(msg)

        flattened: list[CorporateAction] = []
        for events in result.data.values():
            flattened.extend(events)
        return tuple(flattened)


__all__ = ["CorporateActionsQueries"]
