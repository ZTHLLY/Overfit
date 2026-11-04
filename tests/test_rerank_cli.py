"""CLI rerank UX; query configuration is checked before index/embedding work."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from overfit import cli
from overfit.config import EmbedSettings, LLMSettings, RerankSettings, Settings
from overfit.errors import RerankContractError, RerankLogError
from overfit.models import Chunk, RetrievedChunk
from overfit.query import retriever


def _settings(tmp_path, key=""):
    return Settings(
        _env_file=None,
        llm=LLMSettings(_env_file=None),
        embed=EmbedSettings(_env_file=None),
        rerank=RerankSettings(_env_file=None, api_key=SecretStr(key)),
        outputs_dir=tmp_path / "outputs",
        index_dir=tmp_path / "index",
    )


class Store:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def stats(self):
        return {"chunks": 1}


def test_search_without_key_fails_before_opening_index(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    opened = []
    monkeypatch.setattr(cli, "_open_store", lambda *args: opened.append(args))

    result = CliRunner().invoke(cli.app, ["search", "question", "--course", "C"])

    assert result.exit_code == 1
    assert "API key" in result.output
    assert opened == []


def test_search_cli_key_builds_service_and_defaults_to_10(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    marker = object()
    seen = {}

    def make(settings, **overrides):
        seen.update(overrides)
        return marker

    monkeypatch.setattr(cli, "_make_rerank_service", make)
    monkeypatch.setattr(cli, "_open_store", lambda *args: (Store(), object(), _settings(tmp_path)))

    def retrieve(query, store, embedder, **kwargs):
        seen.update(kwargs)
        result = SimpleNamespace(
            status=SimpleNamespace(value="applied"),
            provider="openrouter",
            model="voyageai/rerank-3",
            ranked=(1, 2),
            selected=(1,),
            attempts=1,
            latency_ms=4.2,
            success=True,
        )
        kwargs["on_rerank"](result)
        return [
            RetrievedChunk(
                Chunk("c", "answer text", "notes.pdf", 2),
                vector_score=0.5,
                vector_rank=3,
                rerank_score=0.9,
                rerank_rank=1,
            )
        ]

    monkeypatch.setattr(retriever, "retrieve", retrieve)
    result = CliRunner().invoke(
        cli.app,
        ["search", "question", "--course", "C", "--rerank-api-key", "CLI-SECRET"],
    )

    assert result.exit_code == 0, result.output
    assert seen["api_key"] == "CLI-SECRET"
    assert seen["reranker"] is marker
    assert seen["top_k"] == 10 and seen["fetch_k"] == 40
    assert "[reranker] applied" in result.output
    assert "rerank=+0.900 (r1)" in result.output
    assert "CLI-SECRET" not in result.output


def test_no_topic_coverage_skips_without_key(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_open_store", lambda *args: (Store(), object(), settings))
    monkeypatch.setattr(
        retriever,
        "gather_material",
        lambda *args, **kwargs: [Chunk("c", "course text", "notes.pdf", 1)],
    )

    result = CliRunner().invoke(cli.app, ["coverage", "--course", "C"])

    assert result.exit_code == 0, result.output
    assert "[reranker] skipped reason=no_query" in result.output


def test_topic_coverage_without_key_fails_before_opening_index(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    opened = []
    monkeypatch.setattr(cli, "_open_store", lambda *args: opened.append(args))

    result = CliRunner().invoke(
        cli.app, ["coverage", "--course", "C", "--topic", "clustering"]
    )

    assert result.exit_code == 1
    assert "API key" in result.output
    assert opened == []


def test_invalid_provider_is_a_clean_fail_fast_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path, key="set"))
    opened = []
    monkeypatch.setattr(cli, "_open_store", lambda *args: opened.append(args))

    result = CliRunner().invoke(
        cli.app,
        ["search", "question", "--course", "C", "--rerank-provider", "cohere"],
    )

    assert result.exit_code == 1
    assert "openrouter" in result.output
    assert opened == []


def test_topic_coverage_defaults_to_10_but_explicit_count_overrides(monkeypatch, tmp_path):
    settings = _settings(tmp_path, key="set")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_make_rerank_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_open_store", lambda *args: (Store(), object(), settings))
    counts = []

    def gather(*args, **kwargs):
        counts.append(kwargs["count"])
        return [Chunk("c", "course text", "notes.pdf", 1)]

    monkeypatch.setattr(retriever, "gather_material", gather)
    runner = CliRunner()

    default = runner.invoke(
        cli.app, ["coverage", "--course", "C", "--topic", "clustering"]
    )
    explicit = runner.invoke(
        cli.app,
        ["coverage", "--course", "C", "--topic", "clustering", "--count", "12"],
    )

    assert default.exit_code == 0, default.output
    assert explicit.exit_code == 0, explicit.output
    assert counts == [10, 12]


@pytest.mark.parametrize("error_type", [RerankContractError, RerankLogError])
def test_search_reports_actual_attempts_for_non_external_failure(
    monkeypatch, tmp_path, error_type
):
    settings = _settings(tmp_path, key="set")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_make_rerank_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_open_store", lambda *args: (Store(), object(), settings))
    monkeypatch.setattr(
        retriever,
        "retrieve",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            error_type("fixture", attempts=1)
        ),
    )

    result = CliRunner().invoke(cli.app, ["search", "question", "--course", "C"])

    assert result.exit_code == 1
    assert "[reranker] failed attempts=1; generation aborted" in result.output

