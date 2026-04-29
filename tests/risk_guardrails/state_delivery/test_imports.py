import alphamind.risk_guardrails.state_delivery
import alphamind.risk_guardrails.state_delivery.config


def test_state_delivery_importable() -> None:
    assert alphamind.risk_guardrails.state_delivery is not None


def test_config_module_importable() -> None:
    assert alphamind.risk_guardrails.state_delivery.config is not None
