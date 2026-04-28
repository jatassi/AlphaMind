import alphamind.portfolio_state
import alphamind.portfolio_state.computations
import alphamind.portfolio_state.consumers
import alphamind.portfolio_state.records


def test_portfolio_state_importable() -> None:
    assert alphamind.portfolio_state is not None


def test_records_subpackage_importable() -> None:
    assert alphamind.portfolio_state.records is not None


def test_computations_subpackage_importable() -> None:
    assert alphamind.portfolio_state.computations is not None


def test_consumers_subpackage_importable() -> None:
    assert alphamind.portfolio_state.consumers is not None
