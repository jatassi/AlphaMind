"""Layer-0.5 analyst-only-field normalization for ``submit_envelope`` — ALP-736.

The analyst ``Recommendation`` schema is a superset of the OMS command schema,
and ``pm.md`` instructs the PM to copy instrument / sizing / legs / thesis
straight from the analyst output. Analyst-only leaf fields ride along into the
``extra="forbid"`` command sub-models, where the whole command is rejected —
silently losing a PM-*approved* recommendation when the self-repair retry loop
stalls on a field it doesn't know to drop.

:func:`_strip_analyst_only_command_fields` strips the known analyst-only keys at
the command-parse boundary (after the ALP-700 unwrap, before Pydantic
validation). ``entry_window`` is deliberately NOT stripped — ALP-737 added
``EntryWindow`` to ``OpenCommand`` field-for-field, so it must pass through to
populate the bracket's ``entry_window_deadline``.

Field paths and shapes here mirror the real production failures replayed from
``archive/2026-05-27..28`` (MRVL ``inv-20260528T080000Z``, ZS
``inv-20260528T173000Z``, the event-leg ``order_parameters`` case
``inv-20260527T162007Z``).
"""

from __future__ import annotations

from typing import Any

from alphamind.commands.command_models import OpenCommand
from alphamind.decision.portfolio_manager.submit_envelope.process import (
    _strip_analyst_only_command_fields,
)


def _analyst_superset_open_command() -> dict[str, Any]:
    """A raw OPEN command dict as the PM emits it when copying analyst fields
    verbatim — carrying the analyst-only leaves that ``extra="forbid"`` rejects.

    Modeled on the real MRVL ``inv-20260528T080000Z`` payload: a price + time
    hard leg (each with ``leg_id`` and a legitimate ``order_parameters``), a
    ``position_size`` carrying ``delta_adjusted_exposure`` + ``pct_of_portfolio``,
    and an ``entry_window`` block whose inner shape matches ALP-737's command
    ``EntryWindow``.
    """
    return {
        "command_type": "open",
        "instrument": {"asset_type": "equity", "ticker": "MRVL", "direction": "long"},
        "entry_order": {"type": "market", "limit_price": None, "stop_price": None},
        "position_size": {
            "quantity": 25,
            "dollar_value": 2000,
            "delta_adjusted_exposure": 2000,
            "pct_of_portfolio": 2.0,
        },
        "target": {
            "target_type": "absolute_price",
            "price": 92,
            "dollar_pl_target": 500,
            "pl_percentage": None,
            "pl_dollar": None,
            "order_type": "limit",
        },
        "invalidation_legs": [
            {
                "leg_id": "INV-1",
                "type": "price",
                "is_hard": True,
                "trigger_signal": "underlying_price",
                "condition": {
                    "underlying_trigger": "MRVL",
                    "comparator": "<=",
                    "trigger_price": 73.5,
                },
                "order_parameters": {"order_type": "market", "limit_price": None},
            },
            {
                "leg_id": "INV-2",
                "type": "time",
                "is_hard": True,
                "condition": {"deadline": "2026-05-30T20:00:00Z"},
                "order_parameters": {"order_type": "market", "limit_price": None},
            },
        ],
        "thesis": {
            "summary": "Post-earnings continuation long MRVL.",
            "nature": "directional",
            "components": [
                {
                    "component_type": "entry_rationale",
                    "linked_leg": "entry",
                    "instrument_reference": "MRVL",
                    "narrative": "Capex tailwind absorbed into the gap.",
                    "key_assumptions": ["Continuation holds."],
                }
            ],
        },
        "entry_window": {
            "deadline": "2026-05-28T19:55:00Z",
            "decay_type": "gradual",
            "rationale": "Edge attenuates with time per [SA-TECH-TC-1].",
        },
    }


def test_strip_makes_analyst_superset_open_command_validate() -> None:
    """The canonical regression: an OPEN command carrying analyst-only
    ``delta_adjusted_exposure`` and ``leg_id`` fields fails ``extra="forbid"``
    validation as authored, but validates cleanly after normalization."""
    args = {"commands": [_analyst_superset_open_command()]}

    normalized, _stripped = _strip_analyst_only_command_fields(args)

    # No extra_forbidden — the normalized command is a valid OpenCommand.
    command = OpenCommand.model_validate(normalized["commands"][0])
    assert command.command_type == "open"


def test_entry_window_is_threaded_not_stripped() -> None:
    """ALP-736 / ALP-737: ``entry_window`` is the one analyst block that is
    *kept*, not stripped — ALP-737 added it to ``OpenCommand`` so it must reach
    the bracket. The strip must never touch it (re-dropping it is the exact
    regression ALP-737 fixed)."""
    args = {"commands": [_analyst_superset_open_command()]}

    normalized, stripped = _strip_analyst_only_command_fields(args)

    assert not any("entry_window" in path for path in stripped)
    command = OpenCommand.model_validate(normalized["commands"][0])
    assert command.entry_window is not None
    assert command.entry_window.deadline.isoformat() == "2026-05-28T19:55:00+00:00"
    assert command.entry_window.decay_type == "gradual"


def test_event_leg_order_parameters_stripped_but_price_leg_kept() -> None:
    """An event leg's forbidden ``order_parameters`` is dropped while a price
    leg's required ``order_parameters`` is preserved — the strip is leg-type
    aware (the real ``inv-20260527T162007Z`` event-leg ``order_parameters=null``
    case)."""
    command = _analyst_superset_open_command()
    command["invalidation_legs"].append(
        {
            "leg_id": "INV-3",
            "type": "event",
            "is_hard": False,
            "condition": {"event_description": "Guidance cut at the print."},
            "order_parameters": None,
        }
    )
    args = {"commands": [command]}

    normalized, _stripped = _strip_analyst_only_command_fields(args)

    legs = normalized["commands"][0]["invalidation_legs"]
    price_leg = next(leg for leg in legs if leg["type"] == "price")
    event_leg = next(leg for leg in legs if leg["type"] == "event")
    assert price_leg["order_parameters"] == {"order_type": "market", "limit_price": None}
    assert "order_parameters" not in event_leg
    # And the whole command still validates (the event leg has no order block).
    OpenCommand.model_validate(normalized["commands"][0])


def test_stripped_paths_name_every_removed_key() -> None:
    """The returned paths enumerate exactly the keys removed, so the caller can
    log them — the operator-visible record that normalization fired."""
    args = {"commands": [_analyst_superset_open_command()]}

    _normalized, stripped = _strip_analyst_only_command_fields(args)

    assert set(stripped) == {
        "commands[0].position_size.delta_adjusted_exposure",
        "commands[0].position_size.pct_of_portfolio",
        "commands[0].target.dollar_pl_target",
        "commands[0].invalidation_legs[0].leg_id",
        "commands[0].invalidation_legs[1].leg_id",
    }


def test_target_dollar_pl_target_is_stripped() -> None:
    """The analyst `Target` carries a required `dollar_pl_target` with no command
    `Target` slot — a verbatim copy would reject the whole command, so the strip
    drops it (the full analyst→command schema delta, not only the production
    leaf fields)."""
    args = {"commands": [_analyst_superset_open_command()]}

    normalized, stripped = _strip_analyst_only_command_fields(args)

    assert "commands[0].target.dollar_pl_target" in stripped
    assert "dollar_pl_target" not in normalized["commands"][0]["target"]
    # The price target the command DOES carry is preserved.
    assert normalized["commands"][0]["target"]["price"] == 92
    OpenCommand.model_validate(normalized["commands"][0])


def test_clean_command_is_a_noop() -> None:
    """A command already free of analyst-only fields is returned unchanged
    (same object) with no reported strips."""
    clean = _analyst_superset_open_command()
    del clean["position_size"]["delta_adjusted_exposure"]
    del clean["position_size"]["pct_of_portfolio"]
    del clean["target"]["dollar_pl_target"]
    for leg in clean["invalidation_legs"]:
        del leg["leg_id"]
    args = {"commands": [clean]}

    normalized, stripped = _strip_analyst_only_command_fields(args)

    assert stripped == ()
    # No-op returns the original object unchanged, not a copy.
    assert normalized is args


def test_original_args_are_not_mutated() -> None:
    """Normalization works on a deep copy — the caller's pre-strip payload (the
    forensic ``raw_args_for_log``) is left intact."""
    args = {"commands": [_analyst_superset_open_command()]}

    _normalized, _stripped = _strip_analyst_only_command_fields(args)

    assert args["commands"][0]["position_size"]["delta_adjusted_exposure"] == 2000
    assert args["commands"][0]["invalidation_legs"][0]["leg_id"] == "INV-1"


def test_payload_without_commands_list_is_returned_unchanged() -> None:
    """A payload whose ``commands`` is absent or non-list (a malformed envelope)
    falls through untouched — the strip never raises, leaving the Layer-1 parse
    to surface the structural error."""
    args: dict[str, Any] = {"envelope_id": "ENV-REC-9", "garbage": "value"}

    normalized, stripped = _strip_analyst_only_command_fields(args)

    assert stripped == ()
    assert normalized is args
