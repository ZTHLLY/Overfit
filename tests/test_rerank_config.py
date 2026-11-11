"""Reranker settings load lazily and never expose the secret."""

import pytest
from pydantic import SecretStr

from overfit.config import RerankSettings, Settings
from overfit.errors import RerankConfigurationError


def test_frozen_defaults_do_not_require_key_until_query_use():
    rerank = RerankSettings(_env_file=None)
    settings = Settings(_env_file=None, rerank=rerank)

    assert rerank.provider == "openrouter"
    assert rerank.model == "voyageai/rerank-3"
    assert rerank.base_url == "https://openrouter.ai/api/v1"
    assert rerank.timeout == 30
    assert isinstance(rerank.api_key, SecretStr)
    assert "SECRET" not in repr(settings)
    with pytest.raises(RerankConfigurationError, match="API key"):
        rerank.for_query()


def test_query_settings_accept_env_and_cli_style_copy(monkeypatch):
    monkeypatch.setenv("OVERFIT_RERANK_API_KEY", "SECRET")
    from_env = RerankSettings(_env_file=None)
    ready = from_env.for_query()
    assert ready.api_key.get_secret_value() == "SECRET"

    override = from_env.model_copy(
        update={"api_key": SecretStr("CLI-SECRET"), "timeout": 12.0}
    ).for_query()
    assert override.api_key.get_secret_value() == "CLI-SECRET"
    assert override.timeout == 12
    assert "CLI-SECRET" not in repr(override)


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "cohere"},
        {"base_url": "http://insecure.example/v1"},
        {"model": "   "},
        {"timeout": 0},
    ],
)
def test_invalid_rerank_identity_is_lazy_until_query_use(override):
    rerank = RerankSettings(
        _env_file=None,
        api_key=SecretStr("set"),
        **override,
    )
    settings = Settings(_env_file=None, rerank=rerank)

    assert settings.rerank is rerank
    with pytest.raises(RerankConfigurationError):
        settings.rerank.for_query()

