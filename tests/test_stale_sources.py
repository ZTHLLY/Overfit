"""An indexed file that vanished from disk stops ingestion instead of lingering."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from overfit import cli, config
from overfit.errors import MissingSourcesError
from overfit.ingestion import pipeline
from overfit.storage.store import VectorStore
from tests.test_storage import PROFILE

TEXT = "A fixed condition applies before the conclusion can be used. " * 12


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def embed(self, texts, *, on_progress=None):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


def run(directory, store, embedder, **kwargs):
    return pipeline.ingest(
        directory, store, embedder, chunk_size=100, chunk_overlap=10, **kwargs
    )


@pytest.fixture
def course(tmp_path):
    root = tmp_path / "course"
    (root / "week1").mkdir(parents=True)
    (root / "week1" / "a.txt").write_text(TEXT)
    (root / "week1" / "b.txt").write_text(TEXT + " Another premise.")
    return root


def _indexed(store):
    return sorted(source for source, _ in store.sources())


@pytest.mark.parametrize("change", ["delete", "rename"])
def test_vanished_source_stops_before_any_work(tmp_path, course, change):
    embedder = FakeEmbedder()
    with VectorStore.open(tmp_path / "index.db", PROFILE, create=True) as store:
        run(course, store, embedder)
        assert _indexed(store) == ["week1/a.txt", "week1/b.txt"]
        calls = embedder.calls

        if change == "delete":
            (course / "week1" / "a.txt").unlink()
        else:
            (course / "week1" / "a.txt").rename(course / "week1" / "a_v2.txt")
        (course / "week1" / "new.txt").write_text(TEXT + " A new lecture.")

        with pytest.raises(MissingSourcesError) as caught:
            run(course, store, embedder)

        assert caught.value.missing == ("week1/a.txt",)
        assert "week1/a.txt" in str(caught.value)
        assert "--rebuild" in str(caught.value)
        # Nothing was embedded, added or removed.
        assert embedder.calls == calls
        assert _indexed(store) == ["week1/a.txt", "week1/b.txt"]


def test_narrowed_extensions_are_reported_as_missing(tmp_path, course):
    (course / "notes.md").write_text(TEXT + " Markdown notes.")
    with VectorStore.open(tmp_path / "index.db", PROFILE, create=True) as store:
        run(course, store, FakeEmbedder(), extensions=(".txt", ".md"))

        with pytest.raises(MissingSourcesError, match="EXTENSIONS"):
            run(course, store, FakeEmbedder(), extensions=(".txt",))


def test_long_missing_list_is_truncated(tmp_path):
    root = tmp_path / "course"
    root.mkdir()
    for index in range(8):
        (root / f"l{index}.txt").write_text(TEXT + f" Lecture {index}.")
    with VectorStore.open(tmp_path / "index.db", PROFILE, create=True) as store:
        run(root, store, FakeEmbedder())
        for index in range(7):
            (root / f"l{index}.txt").unlink()

        with pytest.raises(MissingSourcesError) as caught:
            run(root, store, FakeEmbedder())

    assert len(caught.value.missing) == 7
    assert "and 2 more" in str(caught.value)


def test_unchanged_folder_still_ingests(tmp_path, course):
    embedder = FakeEmbedder()
    with VectorStore.open(tmp_path / "index.db", PROFILE, create=True) as store:
        run(course, store, embedder)
        report = run(course, store, embedder)
    assert report.count("cached") == 2


def _course_folder_setup(monkeypatch, tmp_path, course):
    for name in ("EMBED_MODEL", "EXTENSIONS", "COURSES_DIR", "INDEX_DIR", "OUTPUTS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXTENSIONS", ".txt")
    monkeypatch.setattr(config, "env_files", lambda: ())
    monkeypatch.chdir(course)
    config.get_settings.cache_clear()
    embedder = FakeEmbedder()
    embedder.model = config.Settings(_env_file=None).embed.model
    embedder.dimension = PROFILE.embed_dim
    monkeypatch.setattr("overfit.embedding.get_embedder", lambda: embedder)
    return embedder


def test_course_folder_mock_stops_on_a_vanished_source(monkeypatch, tmp_path, course):
    embedder = _course_folder_setup(monkeypatch, tmp_path, course)
    generated = []
    monkeypatch.setattr(
        "overfit.evaluation.live.run_mock_trace",
        lambda *a, **k: generated.append(True) or {
            "error": None, "status": "failed", "run_dir": None, "exit_code": 1,
        },
    )
    runner = CliRunner()
    first = runner.invoke(cli.app, ["mock", "-q", "3"])
    assert first.exit_code == 1, first.output  # fake trace fails; index was built
    assert generated == [True]
    calls = embedder.calls

    (course / "week1" / "a.txt").unlink()
    config.get_settings.cache_clear()
    second = runner.invoke(cli.app, ["mock", "-q", "3"])

    assert second.exit_code == 1
    assert "week1/a.txt" in second.output
    assert "overfit ingest --rebuild" in second.output
    assert "--course" not in second.output
    assert generated == [True]  # no generation after the error
    assert embedder.calls == calls


def test_rebuild_clears_a_vanished_source(monkeypatch, tmp_path, course):
    _course_folder_setup(monkeypatch, tmp_path, course)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["ingest"]).exit_code == 0
    (course / "week1" / "a.txt").unlink()
    config.get_settings.cache_clear()
    assert runner.invoke(cli.app, ["ingest"]).exit_code == 1

    config.get_settings.cache_clear()
    rebuilt = runner.invoke(cli.app, ["ingest", "--rebuild"])

    assert rebuilt.exit_code == 0, rebuilt.output
    assert "1 indexed" in rebuilt.output
