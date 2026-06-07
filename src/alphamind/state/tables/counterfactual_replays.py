"""SQLAlchemy mapping for the ``counterfactual_replays`` table (ALP-556).

One row per replay attempt produced by the counterfactual replay engine.
Append-only; the engine never silently overwrites. One record per
(envelope, replay_kind) tuple enforced by a UniqueConstraint.

CHECK-constraint vocabularies are the enum ``.value`` tokens from the local
enums defined in this module — all five ``UnevaluableReason`` members and all
five ``ExitLeg`` members are enumerated from the start so no later story has
to retroactively widen a CHECK on a fresh metadata-built DB.

The five enums (``ReplayStatus``, ``ReplayKind``, ``UnevaluableReason``,
``ExitLeg``, ``Confidence``), the immutable value object
``CounterfactualReplayRecord``, and the ``ReplayId`` re-export are co-located
here — mirroring the ``AgentCallErrorClass`` + ``AgentCallRecord`` +
``AgentCallsRow`` pattern in ``state/tables/agent_calls.py``. The persistence
layer owns these types: the ``CounterfactualReplays`` table's CHECK-constraint
vocabularies are derived directly from the enum ``.value`` tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from alphamind._kernel.ids import EnvelopeId
from alphamind._kernel.ids import ReplayId as ReplayId  # re-export
from alphamind._kernel.money import Money
from alphamind.persistence.models import Base
from alphamind.state.tables._money_column import DecimalText

# ---------------------------------------------------------------------------
# Enums (ALP-129 vocabulary, owned here after ALP-913)
# ---------------------------------------------------------------------------


class ReplayStatus(StrEnum):
    """Whether the engine produced a counterfactual P/L for the proposal."""

    EVALUATED = "evaluated"
    UNEVALUABLE = "unevaluable"


class ReplayKind(StrEnum):
    """What the replay simulates relative to the PM's decision.

    ``REJECTION`` — PM rejected the proposal; the replay simulates the
    un-rejected form. ``MODIFICATION_ORIGINAL_FORM`` — PM modified the
    proposal; the replay simulates the un-modified original form alongside the
    actual modified-form trade.
    """

    REJECTION = "rejection"
    MODIFICATION_ORIGINAL_FORM = "modification_original_form"


class UnevaluableReason(StrEnum):
    """Why the engine could not produce a counterfactual P/L.

    Set only when :attr:`ReplayStatus.UNEVALUABLE`.

    ``UNSUPPORTED_BRACKET_TYPE`` is part of the design's persisted vocabulary
    and stays defined, but the v2 analyst path does not produce it: analyst
    ``Recommendation`` hard backstops are always underlying-anchored price legs
    or time legs, so the "P/L-on-the-option's-own-price hard backstop" case is
    unreachable from analyst output. The member is reserved; no rule in this
    work tree emits it.

    ``STRATEGIST_POSITION_ACTION_NOT_SUPPORTED`` marks strategist proposals
    whose specific position-action variant the dispatcher cannot replay (e.g.
    ``hold`` / ``maintain`` no-ops, or a ``modify`` pending-order that v2
    declines). Consumed by eligibility (story 04) and the driver (story 08).
    """

    UNSUPPORTED_INSTRUMENT = "unsupported_instrument"
    UNSUPPORTED_BRACKET_TYPE = "unsupported_bracket_type"
    DATA_MISSING = "data_missing"
    CORPORATE_ACTION_IN_WINDOW = "corporate_action_in_window"
    STRATEGIST_POSITION_ACTION_NOT_SUPPORTED = "strategist_position_action_not_supported"


class ExitLeg(StrEnum):
    """Which exit condition fired in the simulated trade.

    ``STRATEGIST_CLOSE_AT_PROPOSAL`` is the one-shot exit leg for strategist
    CLOSE and REDUCE replays, which exit at the proposal-following bar rather
    than via a bracket walk (story 07). ADJUST-BRACKET and ADD strategist
    replays use the ordinary ``TARGET_HIT`` / ``STOP_HIT`` / ``TIME_STOP_FIRED``
    / ``ENTRY_WINDOW_EXPIRED_UNFILLED`` legs.
    """

    TARGET_HIT = "target_hit"
    STOP_HIT = "stop_hit"
    TIME_STOP_FIRED = "time_stop_fired"
    ENTRY_WINDOW_EXPIRED_UNFILLED = "entry_window_expired_unfilled"
    STRATEGIST_CLOSE_AT_PROPOSAL = "strategist_close_at_proposal"


class Confidence(StrEnum):
    """Replay confidence; low-confidence replays are excluded from aggregated
    PM accuracy metrics at query time per the engine's confidence rule.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Immutable value object (ALP-129 record, owned here after ALP-913)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CounterfactualReplayRecord:
    """One counterfactual replay attempt for a PM-rejected or PM-modified proposal.

    The field set mirrors the persisted record in state-persistence.md. The
    cross-field invariants in :meth:`__post_init__` encode the design's "set
    only when …" clauses as type-level guarantees so downstream readers can
    trust the shape without re-checking it.
    """

    replay_id: ReplayId
    pm_decision_envelope_id: EnvelopeId
    replay_kind: ReplayKind
    replay_status: ReplayStatus
    unevaluable_reason: UnevaluableReason | None
    entered: bool | None
    entry_price: Money | None
    entry_timestamp: datetime | None
    entry_slippage: Money | None
    entry_fees: Money | None
    exit_leg: ExitLeg | None
    exit_price: Money | None
    exit_timestamp: datetime | None
    exit_slippage: Money | None
    exit_fees: Money | None
    realized_pl: Money | None
    confidence: Confidence | None
    replay_timestamp: datetime
    replay_data_window_start: datetime | None
    replay_data_window_end: datetime | None
    replay_engine_version: str

    def __post_init__(self) -> None:
        self._check_datetimes_utc()
        if self.replay_status is ReplayStatus.UNEVALUABLE:
            self._check_unevaluable()
        else:
            self._check_evaluated()

    def _check_datetimes_utc(self) -> None:
        """Every non-``None`` datetime field must be tz-aware UTC."""
        fields = (
            ("entry_timestamp", self.entry_timestamp),
            ("exit_timestamp", self.exit_timestamp),
            ("replay_timestamp", self.replay_timestamp),
            ("replay_data_window_start", self.replay_data_window_start),
            ("replay_data_window_end", self.replay_data_window_end),
        )
        for name, value in fields:
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                msg = f"{name} must be tz-aware when not None"
                raise ValueError(msg)

    def _check_unevaluable(self) -> None:
        """UNEVALUABLE: reason required; all outcome/simulation fields ``None``."""
        if self.unevaluable_reason is None:
            msg = "unevaluable_reason is required when replay_status is UNEVALUABLE"
            raise ValueError(msg)
        forbidden = (
            ("entered", self.entered),
            ("entry_price", self.entry_price),
            ("entry_timestamp", self.entry_timestamp),
            ("entry_slippage", self.entry_slippage),
            ("entry_fees", self.entry_fees),
            ("exit_leg", self.exit_leg),
            ("exit_price", self.exit_price),
            ("exit_timestamp", self.exit_timestamp),
            ("exit_slippage", self.exit_slippage),
            ("exit_fees", self.exit_fees),
            ("realized_pl", self.realized_pl),
            ("confidence", self.confidence),
            ("replay_data_window_start", self.replay_data_window_start),
            ("replay_data_window_end", self.replay_data_window_end),
        )
        offenders = [name for name, value in forbidden if value is not None]
        if offenders:
            msg = f"{', '.join(offenders)} must be None when replay_status is UNEVALUABLE"
            raise ValueError(msg)

    def _check_evaluated(self) -> None:
        """EVALUATED: no reason; ``entered`` + ``confidence`` set; entry/exit gated."""
        if self.unevaluable_reason is not None:
            msg = "unevaluable_reason must be None when replay_status is EVALUATED"
            raise ValueError(msg)
        if self.entered is None:
            msg = "entered is required when replay_status is EVALUATED"
            raise ValueError(msg)
        if self.confidence is None:
            msg = "confidence is required when replay_status is EVALUATED"
            raise ValueError(msg)
        if self.entered:
            self._check_entered()
        else:
            self._check_not_entered()

    def _check_entered(self) -> None:
        """``entered`` True: every entry field is non-``None``."""
        entry_fields = (
            ("entry_price", self.entry_price),
            ("entry_timestamp", self.entry_timestamp),
            ("entry_slippage", self.entry_slippage),
            ("entry_fees", self.entry_fees),
        )
        missing = [name for name, value in entry_fields if value is None]
        if missing:
            msg = f"{', '.join(missing)} must be non-None when entered is True"
            raise ValueError(msg)

    def _check_not_entered(self) -> None:
        """``entered`` False: window-expired leg; no exit fields or realized P/L."""
        if self.exit_leg is not ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED:
            msg = "exit_leg must be ENTRY_WINDOW_EXPIRED_UNFILLED when entered is False"
            raise ValueError(msg)
        forbidden = (
            ("exit_price", self.exit_price),
            ("exit_timestamp", self.exit_timestamp),
            ("exit_slippage", self.exit_slippage),
            ("exit_fees", self.exit_fees),
            ("realized_pl", self.realized_pl),
        )
        offenders = [name for name, value in forbidden if value is not None]
        if offenders:
            msg = f"{', '.join(offenders)} must be None when entered is False"
            raise ValueError(msg)


# CHECK-constraint vocabularies, derived from the live enums at import. These
# drive the metadata DDL (Base.metadata.create_all on a fresh DB). The alembic
# migration (a556rp0000dd) hardcodes the same token set as a point-in-time
# snapshot; the head-vs-metadata autogenerate test flags any future enum
# addition that would widen the metadata CHECK without a matching migration.
_REPLAY_KIND_VALUES: tuple[str, ...] = tuple(m.value for m in ReplayKind)
_REPLAY_STATUS_VALUES: tuple[str, ...] = tuple(m.value for m in ReplayStatus)
_UNEVALUABLE_REASON_VALUES: tuple[str, ...] = tuple(m.value for m in UnevaluableReason)
_EXIT_LEG_VALUES: tuple[str, ...] = tuple(m.value for m in ExitLeg)
_CONFIDENCE_VALUES: tuple[str, ...] = tuple(m.value for m in Confidence)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class CounterfactualReplays(Base):
    """Append-only per-replay row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md``
    § Counterfactual replays.
    """

    __tablename__ = "counterfactual_replays"

    replay_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Logical reference to the originating ``pm_decision`` activity-log entry's
    # envelope id. No DB-level ForeignKey: ``activity_log`` carries the
    # envelope id inside ``detail_json`` (PMDecisionDetail.envelope_id), not as
    # a top-level column, so there is no column to constrain against. The
    # engine driver (story 08) resolves the referent at query time.
    pm_decision_envelope_id: Mapped[str] = mapped_column(Text, nullable=False)
    replay_kind: Mapped[str] = mapped_column(Text, nullable=False)
    replay_status: Mapped[str] = mapped_column(Text, nullable=False)
    unevaluable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    entered: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    entry_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    entry_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_slippage: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    entry_fees: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    exit_leg: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    exit_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_slippage: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    exit_fees: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    realized_pl: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    replay_data_window_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_data_window_end: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_engine_version: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "pm_decision_envelope_id",
            "replay_kind",
            name="uq_counterfactual_replays_envelope_kind",
        ),
        CheckConstraint(
            _check_in("replay_kind", _REPLAY_KIND_VALUES),
            name="ck_counterfactual_replays_replay_kind",
        ),
        CheckConstraint(
            _check_in("replay_status", _REPLAY_STATUS_VALUES),
            name="ck_counterfactual_replays_replay_status",
        ),
        CheckConstraint(
            "unevaluable_reason IS NULL OR "
            + _check_in("unevaluable_reason", _UNEVALUABLE_REASON_VALUES),
            name="ck_counterfactual_replays_unevaluable_reason",
        ),
        CheckConstraint(
            f"exit_leg IS NULL OR {_check_in('exit_leg', _EXIT_LEG_VALUES)}",
            name="ck_counterfactual_replays_exit_leg",
        ),
        CheckConstraint(
            f"confidence IS NULL OR {_check_in('confidence', _CONFIDENCE_VALUES)}",
            name="ck_counterfactual_replays_confidence",
        ),
        Index("ix_counterfactual_replays_envelope", "pm_decision_envelope_id"),
        Index(
            "ix_counterfactual_replays_status_timestamp",
            "replay_status",
            "replay_timestamp",
        ),
    )


__all__ = [
    "Confidence",
    "CounterfactualReplayRecord",
    "CounterfactualReplays",
    "ExitLeg",
    "ReplayId",
    "ReplayKind",
    "ReplayStatus",
    "UnevaluableReason",
]
