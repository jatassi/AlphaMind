import alphamind
import alphamind.analysis
import alphamind.collector
import alphamind.command_center
import alphamind.config
import alphamind.data_sources
import alphamind.decision
import alphamind.distillation
import alphamind.execution
import alphamind.feedback_loop
import alphamind.persistence
import alphamind.pipeline
import alphamind.risk_guardrails


def test_top_level_package_importable() -> None:
    assert alphamind is not None


def test_config_importable() -> None:
    assert alphamind.config is not None


def test_data_sources_importable() -> None:
    assert alphamind.data_sources is not None


def test_persistence_importable() -> None:
    assert alphamind.persistence is not None


def test_collector_importable() -> None:
    assert alphamind.collector is not None


def test_distillation_importable() -> None:
    assert alphamind.distillation is not None


def test_analysis_importable() -> None:
    assert alphamind.analysis is not None


def test_decision_importable() -> None:
    assert alphamind.decision is not None


def test_execution_importable() -> None:
    assert alphamind.execution is not None


def test_risk_guardrails_importable() -> None:
    assert alphamind.risk_guardrails is not None


def test_pipeline_importable() -> None:
    assert alphamind.pipeline is not None


def test_command_center_importable() -> None:
    assert alphamind.command_center is not None


def test_feedback_loop_importable() -> None:
    assert alphamind.feedback_loop is not None
