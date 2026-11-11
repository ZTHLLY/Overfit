"""Independent offline review regressions for the provider-construction boundary."""

import pytest


def test_default_suite_blocks_real_sync_sdk_construction():
    from openai import OpenAI

    with pytest.raises(AssertionError, match="Real provider clients are forbidden"):
        OpenAI(api_key="fixture", base_url="https://fixture.invalid/v1")


def test_default_suite_blocks_real_async_sdk_construction():
    from openai import AsyncOpenAI

    with pytest.raises(AssertionError, match="Real provider clients are forbidden"):
        AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1")
