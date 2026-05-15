"""Verify the ``data_sources/_common/`` package layout — ALP-473.

The legacy ``data_sources/_common.py`` is split into focused submodules
(per the audit's grouping). The package's ``__init__.py`` re-exports every
symbol the 22 known importers depend on so existing
``from alphamind.data_sources._common import X`` calls keep working.
"""

from __future__ import annotations


class TestBackwardCompatReexports:
    """Every public name from the legacy module remains importable."""

    def test_retry_symbols(self) -> None:
        from alphamind.data_sources._common import RetryShape, with_retries

        assert callable(with_retries)
        assert RetryShape.critical.value == "critical"

    def test_rate_limit_symbol(self) -> None:
        from alphamind.data_sources._common import RateLimiter

        assert callable(RateLimiter)

    def test_run_tracking_symbols(self) -> None:
        from alphamind.data_sources._common import (
            RunState,
            default_session_factory,
            track_run,
        )

        assert callable(track_run)
        assert callable(default_session_factory)
        assert callable(RunState)

    def test_resume_helper(self) -> None:
        from alphamind.data_sources._common import resume_since

        assert callable(resume_since)

    def test_universe_helper(self) -> None:
        from alphamind.data_sources._common import active_universe_tickers

        assert callable(active_universe_tickers)

    def test_config_symbols(self) -> None:
        from alphamind.data_sources._common import AlphaMindConfig, load_config

        assert callable(load_config)
        assert callable(AlphaMindConfig)

    def test_headline_taxonomy_symbols(self) -> None:
        from alphamind.data_sources._common import (
            HeadlineType,
            decode_topic_tags,
            normalize_vendor_tags,
        )

        assert HeadlineType.BREAKING.value == "breaking"
        assert callable(normalize_vendor_tags)
        assert callable(decode_topic_tags)


class TestSubmoduleHomes:
    """Each concern lives in its own focused submodule."""

    def test_retry_module_carries_retry_symbols(self) -> None:
        from alphamind.data_sources._common.retry import RetryShape, with_retries

        assert callable(with_retries)
        assert RetryShape.critical.value == "critical"

    def test_rate_limit_module(self) -> None:
        from alphamind.data_sources._common.rate_limit import RateLimiter

        assert callable(RateLimiter)

    def test_run_tracking_module(self) -> None:
        from alphamind.data_sources._common.run_tracking import (
            RunState,
            default_session_factory,
            track_run,
        )

        assert callable(track_run)
        assert callable(default_session_factory)
        assert callable(RunState)

    def test_resume_module(self) -> None:
        from alphamind.data_sources._common.resume import resume_since

        assert callable(resume_since)

    def test_universe_module(self) -> None:
        from alphamind.data_sources._common.universe import active_universe_tickers

        assert callable(active_universe_tickers)

    def test_config_module(self) -> None:
        from alphamind.data_sources._common.config import AlphaMindConfig, load_config

        assert callable(load_config)
        assert callable(AlphaMindConfig)

    def test_env_example_helpers_already_hoisted(self) -> None:
        """``_ENV_EXAMPLE_KEYS`` / ``_load_env_example_keys`` already live at
        ``alphamind.config.models`` (pre-existing hoist). The audit listed it
        among ``_common.py``'s concerns but the symbols were never in this
        file — so no ``_common/env_example.py`` submodule is needed."""
        from alphamind.config.models import _ENV_EXAMPLE_KEYS, _load_env_example_keys

        assert callable(_load_env_example_keys)
        assert isinstance(_ENV_EXAMPLE_KEYS, frozenset)


class TestNewsTypesMigration:
    """``HeadlineType`` and tag-normalization helpers live in news domain."""

    def test_news_types_module_carries_headline_type(self) -> None:
        from alphamind.data_sources.news.types import (
            HeadlineType,
            decode_topic_tags,
            normalize_vendor_tags,
        )

        assert HeadlineType.BREAKING.value == "breaking"
        assert callable(normalize_vendor_tags)
        assert callable(decode_topic_tags)
