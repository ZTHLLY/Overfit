"""CLI authorization/default rules that must hold before provider setup."""

from types import SimpleNamespace

from typer.testing import CliRunner

from overfit import cli
from overfit.storage.store import IndexProfile, VectorStore


def test_topic_requires_explicit_question_count_before_settings_access(monkeypatch):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings must stay cold")),
    )

    result = CliRunner().invoke(
        cli.app, ["mock", "--course", "COURSE", "--topic", "clustering"]
    )

    assert result.exit_code != 0
    assert "--questions" in result.output


def test_no_topic_still_resolves_omitted_count_to_thirty(monkeypatch):
    seen = {}

    def fake_trace(settings, open_store, **kwargs):
        seen.update(kwargs)
        return {
            "status": "empty",
            "exit_code": 0,
            "error": None,
            "run_dir": None,
        }

    monkeypatch.setattr(cli, "get_settings", lambda: object())
    monkeypatch.setattr("overfit.evaluation.live.run_mock_trace", fake_trace)

    result = CliRunner().invoke(
        cli.app, ["mock", "--course", "COURSE", "--trace"]
    )

    assert result.exit_code == 0, result.output
    assert seen["questions"] == 30


def test_no_topic_mock_opens_existing_index_without_embedding_probe(
    tmp_path, monkeypatch
):
    path = tmp_path / "COURSE.db"
    profile = IndexProfile("fake-embed", 2, 100, 10)
    VectorStore.open(path, profile, create=True).close()

    class EmbedderWithoutProbe:
        model = "fake-embed"

        @property
        def dimension(self):
            raise AssertionError("read-only no-topic mock must not probe embedding")

    settings = SimpleNamespace(
        embed=SimpleNamespace(model="fake-embed"),
        chunk_size=100,
        chunk_overlap=10,
        pdf_backend="pypdf",
        db_path=lambda _course: path,
    )

    def fake_trace(_settings, open_store, **_kwargs):
        store, embedder, resolved = open_store("COURSE")
        store.close()
        assert isinstance(embedder, EmbedderWithoutProbe)
        assert resolved is settings
        return {"status": "empty", "exit_code": 0, "error": None, "run_dir": None}

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("overfit.embedding.get_embedder", EmbedderWithoutProbe)
    monkeypatch.setattr("overfit.evaluation.live.run_mock_trace", fake_trace)

    result = CliRunner().invoke(
        cli.app, ["mock", "--course", "COURSE", "--questions", "30", "--trace"]
    )

    assert result.exit_code == 0, result.output
