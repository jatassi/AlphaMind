import alphamind.risk_guardrails.breach_behavior
import alphamind.risk_guardrails.breach_behavior.config


def test_breach_behavior_importable() -> None:
    assert alphamind.risk_guardrails.breach_behavior is not None


def test_config_module_importable() -> None:
    assert alphamind.risk_guardrails.breach_behavior.config is not None
