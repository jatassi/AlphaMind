"""Tests for src/alphamind/config/models/venue.py — venue.yaml model (story 03d)."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def _valid_credentials_raw() -> dict[str, str]:
    return {
        "rest_url": "https://paper-api.alpaca.markets",
        "ws_url": "wss://paper-api.alpaca.markets/stream",
        "api_key_env": "ALPACA_PAPER_KEY",
        "api_secret_env": "ALPACA_PAPER_SECRET",
    }


def _valid_venue_raw() -> dict[str, Any]:
    return {
        "alpaca": {
            "paper": _valid_credentials_raw(),
            "live": {
                "rest_url": "https://api.alpaca.markets",
                "ws_url": "wss://api.alpaca.markets/stream",
                "api_key_env": "ALPACA_LIVE_KEY",
                "api_secret_env": "ALPACA_LIVE_SECRET",
            },
            "rate_limit_per_minute": 200,
        },
        "session_hours": {
            "regular": {"open": "09:30", "close": "16:00"},
            "pre_market": {"open": "04:00", "close": "09:30"},
            "after_hours": {"open": "16:00", "close": "20:00"},
        },
    }


def test_venue_yaml_parses_cleanly_and_exposes_credentials() -> None:
    from alphamind.config.models import VenueConfig

    data = load_yaml(CONFIG_DIR / "venue.yaml")
    config = VenueConfig.model_validate(data)
    assert config.alpaca.paper.api_key_env
    assert config.alpaca.live.api_key_env


def test_venue_rejects_non_https_rest_url() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    raw["alpaca"]["paper"]["rest_url"] = "http://paper-api.alpaca.markets"
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)


def test_venue_rejects_non_wss_ws_url() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    raw["alpaca"]["paper"]["ws_url"] = "ws://paper-api.alpaca.markets/stream"
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)


def test_venue_rejects_lowercase_api_key_env() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    raw["alpaca"]["paper"]["api_key_env"] = "alpaca_paper_key"
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)


def test_venue_rejects_session_hour_25() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    raw["session_hours"]["regular"]["open"] = "25:00"
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)


def test_venue_rejects_missing_live_block() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    del raw["alpaca"]["live"]
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)


def test_venue_rejects_missing_pre_market_block() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    del raw["session_hours"]["pre_market"]
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)


def test_venue_rejects_single_digit_hour() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    raw["session_hours"]["regular"]["open"] = "9:30"
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)


def test_venue_models_are_frozen() -> None:
    from alphamind.config.models import (
        Alpaca,
        AlpacaCredentials,
        SessionHours,
        SessionWindow,
        VenueConfig,
    )

    data = load_yaml(CONFIG_DIR / "venue.yaml")
    config = VenueConfig.model_validate(data)

    # Outer aggregate
    assert isinstance(config, VenueConfig)
    with pytest.raises(ValidationError):
        config.__setattr__("alpaca", None)

    # Alpaca block
    assert isinstance(config.alpaca, Alpaca)
    with pytest.raises(ValidationError):
        config.alpaca.__setattr__("rate_limit_per_minute", 1)

    # Credentials
    assert isinstance(config.alpaca.paper, AlpacaCredentials)
    with pytest.raises(ValidationError):
        config.alpaca.paper.__setattr__("api_key_env", "MUTATED")

    # Session hours and windows
    assert isinstance(config.session_hours, SessionHours)
    with pytest.raises(ValidationError):
        config.session_hours.__setattr__("regular", None)

    assert isinstance(config.session_hours.regular, SessionWindow)
    with pytest.raises(ValidationError):
        config.session_hours.regular.__setattr__("open", "00:00")


def test_venue_rejects_zero_rate_limit() -> None:
    from alphamind.config.models import VenueConfig

    raw = _valid_venue_raw()
    raw["alpaca"]["rate_limit_per_minute"] = 0
    with pytest.raises(ValidationError):
        VenueConfig.model_validate(raw)
