import alphamind
import alphamind.collector
import alphamind.config
import alphamind.data_sources
import alphamind.persistence


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
