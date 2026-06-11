"""Options chain-slice read + selection core — ALP-948.

The decision layer's contract-level read over ``options_contract_snapshots``:
a filtered per-underlying chain slice (the ``retrieve_options_chain`` MCP
tool's data source), a per-contract latest-quote lookup (the analyst
validator's premium anchor), and a per-ticker options context (IV rank +
liquid expirations) rendered into the REFERENCE PRICES block.

Selection (:func:`select_chain_slice`) is a pure function over plain
:class:`ContractQuote` tuples so the strike-band / expiration-window /
open-interest policy is unit-testable with no DB session. The SQL shell
(:class:`SqlOptionsChainReader`) follows the session conventions of
:mod:`alphamind.state.repository.sql_option_price_provider` — one fresh sync
``Session`` per read against the WAL-mode SQLite database.

IV rank reuses the q3 pure compute read-only:
:func:`~alphamind.distillation.q3.atm_iv_baseline_loaders.load_atm_iv_history_by_ticker`
plus :func:`~alphamind.distillation.q3.atm_iv_baseline_compute.compute_atm_iv_baseline`
— explicitly NOT ``load_and_refresh_atm_iv_baselines``, whose upsert must not
run a second time per invocation.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import OccSymbol, make_occ_symbol
from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.q3.atm_iv_baseline_compute import compute_atm_iv_baseline
from alphamind.distillation.q3.atm_iv_baseline_loaders import load_atm_iv_history_by_ticker
from alphamind.persistence.models import OptionsContracts, OptionsContractSnapshots

if TYPE_CHECKING:
    from alphamind.config.models.options_chain import OptionsChainConfig

__all__ = [
    "ChainFilterParams",
    "ChainSlice",
    "ContractQuote",
    "OptionsChainReader",
    "SqlOptionsChainReader",
    "TickerOptionsContext",
    "build_options_context",
    "occ_symbol_for_contract",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IV-rank window policy
# ---------------------------------------------------------------------------

# Mirror q3's ATM-IV baseline window (``q3/_loaders._ATM_IV_HISTORY_DAYS`` /
# ``_ATM_IV_MIN_OBSERVATIONS``). Restated here rather than imported because
# those names are private to the q3 loader composition; the IVr column is
# advisory context, so a future drift between the two consumers degrades
# gracefully rather than breaking a contract.
_IV_RANK_WINDOW_DAYS: int = 252
_IV_RANK_MIN_OBSERVATIONS: int = 60


# ---------------------------------------------------------------------------
# Typed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainFilterParams:
    """Config-owned chain-slice selection policy (``options_chain`` section).

    Filter values are operator-tunable configuration, never LLM-supplied —
    the ``retrieve_options_chain`` tool's input schema carries only the
    underlying ticker.
    """

    strike_band_pct: float
    min_days_to_expiration: int
    max_days_to_expiration: int
    min_open_interest: int
    max_contracts_rendered: int

    @classmethod
    def from_config(cls, config: OptionsChainConfig) -> ChainFilterParams:
        """Project the ``options_chain`` config section onto the filter policy.

        ``premium_staleness_tolerance_pct`` is deliberately excluded — it is
        validator policy, not chain-selection policy, and travels to
        ``validate_analyst_output`` separately.
        """
        return cls(
            strike_band_pct=config.strike_band_pct,
            min_days_to_expiration=config.min_days_to_expiration,
            max_days_to_expiration=config.max_days_to_expiration,
            min_open_interest=config.min_open_interest,
            max_contracts_rendered=config.max_contracts_rendered,
        )


@dataclass(frozen=True, slots=True)
class ContractQuote:
    """Latest usable snapshot for one option contract."""

    occ_symbol: OccSymbol
    underlying: str
    expiration: date
    strike: float
    contract_type: Literal["call", "put"]
    bid: float
    ask: float
    implied_volatility: float
    delta: float
    open_interest: int
    snapshot_ts: datetime

    @property
    def nbbo_mid(self) -> float:
        """Bid/ask midpoint — the premium anchor the validator checks against."""
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True, slots=True)
class ChainSlice:
    """Filtered per-underlying chain view the chain tool renders.

    ``omitted`` is a human-readable note naming what the filters dropped —
    the slice never silently truncates. ``None`` when nothing was dropped.
    """

    underlying: str
    spot: float
    as_of: datetime
    contracts: tuple[ContractQuote, ...]
    omitted: str | None


@dataclass(frozen=True, slots=True)
class TickerOptionsContext:
    """Per-ticker options context rendered into the REFERENCE PRICES block.

    ``iv_rank`` is the ATM-IV percentile rank (0-100) from the q3 pure
    compute, ``None`` while the baseline is not yet calibrated or the rank is
    undefined. ``liquid_expirations`` are the expirations surviving the
    chain-slice filters.
    """

    iv_rank: float | None
    liquid_expirations: tuple[date, ...]


# ---------------------------------------------------------------------------
# OCC symbol construction
# ---------------------------------------------------------------------------


def occ_symbol_for_contract(
    *,
    underlying: str,
    expiration: date,
    strike: float,
    contract_type: Literal["call", "put"],
) -> OccSymbol:
    """Build the compressed OCC symbol for a single-leg contract.

    Format ``{ROOT}{YYMMDD}{C|P}{strike_milli:08d}`` — the collector's
    Polygon ``contract_ticker`` minus its ``O:`` prefix (see
    ``risk_guardrails.guardrail_evaluation.iv_sourcing._polygon_options_contract_ticker``).
    Share-class dots drop per OCC convention (``BRK.B`` → ``BRKB``); the
    strike is rounded to thousandths before formatting to avoid binary-float
    drift (``12.50`` → ``00012500``).
    """
    root = underlying.upper().replace(".", "")
    expiry = expiration.strftime("%y%m%d")
    cp = "C" if contract_type == "call" else "P"
    strike_milli = round(strike * 1000)
    return make_occ_symbol(f"{root}{expiry}{cp}{strike_milli:08d}")


def _polygon_contract_ticker(occ: OccSymbol) -> str:
    """The collector-written ``contract_ticker`` key for a compressed OCC symbol."""
    return f"O:{occ}"


# ---------------------------------------------------------------------------
# Pure selection core
# ---------------------------------------------------------------------------


def select_chain_slice(
    rows: tuple[ContractQuote, ...],
    *,
    underlying: str,
    spot: float,
    as_of: datetime,
    params: ChainFilterParams,
) -> ChainSlice:
    """Apply the configured strike band, expiration window, open-interest
    floor, and contract cap to *rows*.

    Pure function — *as_of* anchors the days-to-expiration window (the
    caller's clock; the SQL shell passes its injected ``now``). When the
    survivors exceed ``max_contracts_rendered``, the cap keeps the contracts
    closest to the money; every drop is named in ``ChainSlice.omitted``.
    """
    as_of_date = as_of.astimezone(UTC).date()
    band = spot * params.strike_band_pct / 100.0

    outside_band = 0
    outside_window = 0
    below_oi_floor = 0
    survivors: list[ContractQuote] = []
    for row in rows:
        if abs(row.strike - spot) > band:
            outside_band += 1
            continue
        dte = (row.expiration - as_of_date).days
        if dte < params.min_days_to_expiration or dte > params.max_days_to_expiration:
            outside_window += 1
            continue
        if row.open_interest < params.min_open_interest:
            below_oi_floor += 1
            continue
        survivors.append(row)

    beyond_cap = max(0, len(survivors) - params.max_contracts_rendered)
    if beyond_cap:
        survivors.sort(key=lambda r: (abs(r.strike - spot), r.expiration, r.contract_type))
        survivors = survivors[: params.max_contracts_rendered]
    survivors.sort(key=lambda r: (r.expiration, r.strike, r.contract_type))

    notes: list[str] = []
    if outside_band:
        notes.append(f"{outside_band} outside the ±{params.strike_band_pct:g}% strike band")
    if outside_window:
        notes.append(
            f"{outside_window} outside the "
            f"{params.min_days_to_expiration}-{params.max_days_to_expiration} DTE window"
        )
    if below_oi_floor:
        notes.append(f"{below_oi_floor} below the open-interest floor {params.min_open_interest}")
    if beyond_cap:
        notes.append(
            f"{beyond_cap} furthest-from-money beyond the "
            f"{params.max_contracts_rendered}-contract cap"
        )
    omitted = f"omitted: {', '.join(notes)}" if notes else None

    return ChainSlice(
        underlying=underlying,
        spot=spot,
        as_of=as_of,
        contracts=tuple(survivors),
        omitted=omitted,
    )


# ---------------------------------------------------------------------------
# Reader Protocol
# ---------------------------------------------------------------------------


class OptionsChainReader(Protocol):
    """Contract-level options read surface for the decision layer."""

    def chain_slice(self, underlying: str) -> ChainSlice | None:
        """The filtered chain slice for *underlying*, or ``None`` without usable data."""
        ...

    def latest_quote(self, occ: OccSymbol) -> ContractQuote | None:
        """The latest usable snapshot for one contract, or ``None``."""
        ...

    def options_context(self, underlying: str) -> TickerOptionsContext | None:
        """IV rank + liquid expirations for *underlying*, or ``None`` without a chain."""
        ...


# ---------------------------------------------------------------------------
# SQL shell
# ---------------------------------------------------------------------------


def _parse_snapshot_ts(raw: str) -> datetime:
    """Parse the collector's snapshot timestamp into a tz-aware UTC datetime.

    Kept local rather than imported from ``sql_option_price_provider`` —
    importing that module transitively reaches ``sql_repository`` and its
    execution-layer aggregates, which breaks the ``decision-not-execution``
    import contract for this module's decision-layer consumers. Mirrors the
    same kept-local rationale in
    ``execution.continuous_monitor.greeks_refresh.iv_provider._parse_snapshot_ts``.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


_SNAPSHOT_COLUMNS = (
    OptionsContractSnapshots.contract_ticker,
    OptionsContractSnapshots.snapshot_ts,
    OptionsContractSnapshots.bid,
    OptionsContractSnapshots.ask,
    OptionsContractSnapshots.implied_volatility,
    OptionsContractSnapshots.delta,
    OptionsContractSnapshots.open_interest,
    OptionsContracts.underlying_ticker,
    OptionsContracts.expiration_date,
    OptionsContracts.strike_price,
    OptionsContracts.contract_type,
)


class SqlOptionsChainReader:
    """:class:`OptionsChainReader` over ``options_contract_snapshots``.

    Reads the latest snapshot per contract (correlated ``MAX(snapshot_ts)``
    subquery, mirroring :class:`SqlOptionPriceProvider`) joined to
    ``options_contracts`` for the strike / expiration / type reference
    fields. One fresh sync ``Session`` per read; WAL-mode SQLite permits the
    concurrent reader.

    ``params`` is exposed read-only so the subprocess transport's pickle shim
    can reconstruct an equivalent reader against the worker's own session
    factory (see ``analysis._sdk_subprocess``).
    """

    def __init__(
        self,
        *,
        sync_session_factory: sessionmaker[Session],
        params: ChainFilterParams,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sync_session_factory = sync_session_factory
        self._params = params
        self._now = now

    @property
    def params(self) -> ChainFilterParams:
        """The chain-slice selection policy this reader applies."""
        return self._params

    # -- chain_slice --------------------------------------------------------

    def chain_slice(self, underlying: str) -> ChainSlice | None:
        with self._sync_session_factory() as session:
            return self._chain_slice(session, underlying)

    def _chain_slice(self, session: Session, underlying: str) -> ChainSlice | None:
        latest_subq = (
            select(
                OptionsContractSnapshots.contract_ticker.label("ct"),
                func.max(OptionsContractSnapshots.snapshot_ts).label("max_ts"),
            )
            .where(OptionsContractSnapshots.underlying_ticker == underlying)
            .group_by(OptionsContractSnapshots.contract_ticker)
            .subquery()
        )
        stmt = (
            select(*_SNAPSHOT_COLUMNS, OptionsContractSnapshots.underlying_price)
            .join(
                latest_subq,
                (OptionsContractSnapshots.contract_ticker == latest_subq.c.ct)
                & (OptionsContractSnapshots.snapshot_ts == latest_subq.c.max_ts),
            )
            .join(
                OptionsContracts,
                OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
            )
        )

        quotes: list[ContractQuote] = []
        unusable = 0
        spot: float | None = None
        spot_ts: datetime | None = None
        for row in session.execute(stmt).all():
            quote = _quote_from_row(row)
            if quote is None:
                unusable += 1
            else:
                quotes.append(quote)
            # Spot rides on the freshest snapshot row carrying an
            # underlying_price — usable-quote status is irrelevant to it.
            row_ts = _parse_snapshot_ts(row.snapshot_ts)
            if row.underlying_price is not None and (spot_ts is None or row_ts > spot_ts):
                spot = float(row.underlying_price)
                spot_ts = row_ts

        if spot is None or spot <= 0:
            return None

        chain = select_chain_slice(
            tuple(quotes),
            underlying=underlying,
            spot=spot,
            as_of=self._now(),
            params=self._params,
        )
        if unusable:
            note = f"{unusable} contract(s) without a usable two-sided quote"
            omitted = f"{chain.omitted}, {note}" if chain.omitted else f"omitted: {note}"
            chain = dataclasses.replace(chain, omitted=omitted)
        return chain

    # -- latest_quote -------------------------------------------------------

    def latest_quote(self, occ: OccSymbol) -> ContractQuote | None:
        """The latest USABLE snapshot for *occ* — a newer garbage row (missing
        or crossed quote, missing IV/delta) must not mask an older usable one,
        or the validator would reject a premium the chain tool itself anchored.
        The SQL filter mirrors :func:`_quote_from_row`'s usability predicate.
        """
        contract_ticker = _polygon_contract_ticker(occ)
        stmt = (
            select(*_SNAPSHOT_COLUMNS)
            .join(
                OptionsContracts,
                OptionsContracts.contract_ticker == OptionsContractSnapshots.contract_ticker,
            )
            .where(
                OptionsContractSnapshots.contract_ticker == contract_ticker,
                OptionsContractSnapshots.bid.is_not(None),
                OptionsContractSnapshots.ask.is_not(None),
                OptionsContractSnapshots.bid >= 0,
                OptionsContractSnapshots.ask > 0,
                OptionsContractSnapshots.bid <= OptionsContractSnapshots.ask,
                OptionsContractSnapshots.implied_volatility.is_not(None),
                OptionsContractSnapshots.delta.is_not(None),
            )
            .order_by(OptionsContractSnapshots.snapshot_ts.desc())
            .limit(1)
        )
        with self._sync_session_factory() as session:
            row = session.execute(stmt).first()
        if row is None:
            return None
        return _quote_from_row(row)

    # -- options_context ----------------------------------------------------

    def options_context(self, underlying: str) -> TickerOptionsContext | None:
        # One session serves both the chain read and the IV-history read —
        # build_options_context loops this per active-universe ticker, so the
        # per-call session count matters.
        with self._sync_session_factory() as session:
            chain = self._chain_slice(session, underlying)
            if chain is None or not chain.contracts:
                return None
            iv_rank = self._iv_rank(session, underlying)
        expirations = tuple(sorted({quote.expiration for quote in chain.contracts}))
        return TickerOptionsContext(iv_rank=iv_rank, liquid_expirations=expirations)

    def _iv_rank(self, session: Session, underlying: str) -> float | None:
        """Read-only IV-rank via the q3 pure compute — no baseline upsert."""
        as_of_iso = self._now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        history = load_atm_iv_history_by_ticker(
            session,
            ticker_scope=(underlying,),
            as_of=as_of_iso,
            window_days=_IV_RANK_WINDOW_DAYS,
        )[underlying]
        result = compute_atm_iv_baseline(
            history,
            window_days=_IV_RANK_WINDOW_DAYS,
            min_observations=_IV_RANK_MIN_OBSERVATIONS,
        )
        if result.rank.state is not CalibrationState.CALIBRATED or result.rank.value is None:
            return None
        percentile = result.rank.value.get("iv_rank_percentile")
        return float(percentile) if percentile is not None else None


def _quote_from_row(row: Any) -> ContractQuote | None:
    """Build a :class:`ContractQuote` from a row selecting ``_SNAPSHOT_COLUMNS``.

    Fields are read by column label (order-independent — a future reorder of
    ``_SNAPSHOT_COLUMNS`` cannot silently mis-assign them). Returns ``None``
    for an unusable row: a missing or crossed two-sided quote, a missing IV
    or delta, or a contract ticker outside the OCC pattern. A zero bid alone
    is usable — deep-OTM contracts legitimately quote ``bid=0``.
    """
    bid, ask = row.bid, row.ask
    if bid is None or ask is None or float(ask) <= 0 or float(bid) < 0 or float(bid) > float(ask):
        return None
    if row.implied_volatility is None or row.delta is None:
        return None
    if row.contract_type not in ("call", "put"):
        return None
    try:
        occ = make_occ_symbol(str(row.contract_ticker).removeprefix("O:"))
    except ValueError:
        logger.warning("skipping non-OCC contract_ticker %r", row.contract_ticker)
        return None
    return ContractQuote(
        occ_symbol=occ,
        underlying=str(row.underlying_ticker),
        expiration=date.fromisoformat(str(row.expiration_date)),
        strike=float(row.strike_price),
        contract_type=row.contract_type,
        bid=float(bid),
        ask=float(ask),
        implied_volatility=float(row.implied_volatility),
        delta=float(row.delta),
        open_interest=int(row.open_interest) if row.open_interest is not None else 0,
        snapshot_ts=_parse_snapshot_ts(str(row.snapshot_ts)),
    )


# ---------------------------------------------------------------------------
# Per-invocation context assembly
# ---------------------------------------------------------------------------


def build_options_context(
    reader: OptionsChainReader,
    *,
    tickers: Iterable[str],
) -> Mapping[str, TickerOptionsContext]:
    """Assemble the per-ticker options-context map for the REFERENCE PRICES render.

    Tickers without a usable chain are simply absent — their reference-price
    line stays the bare price line.
    """
    context: dict[str, TickerOptionsContext] = {}
    for ticker in tickers:
        ticker_context = reader.options_context(ticker)
        if ticker_context is not None:
            context[ticker] = ticker_context
    return context
