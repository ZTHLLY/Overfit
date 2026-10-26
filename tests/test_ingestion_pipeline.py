"""Processing cache invalidation without models, PDFs, or persistent indexes."""

from types import SimpleNamespace

import pytest

from overfit.errors import OverfitError
from overfit.ingestion import pipeline
from overfit.storage.store import VectorStore
from tests.test_storage import PROFILE, legacy_index


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def embed(self, texts, *, on_progress=None):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


def run(directory, store, embedder):
    return pipeline.ingest(directory, store, embedder, chunk_size=100, chunk_overlap=10)


def test_ingest_cache_checks_content_and_processing_identity(tmp_path, monkeypatch):
    source = tmp_path / "course"
    source.mkdir()
    path = source / "lecture.txt"
    path.write_text(
        "A fixed condition applies before the conclusion can be used. " * 12
    )
    embedder = FakeEmbedder()
    monkeypatch.setattr(pipeline, "processing_identity", lambda *args: "parser-v1")
    with VectorStore.open(tmp_path / "index.db", PROFILE, create=True) as store:
        assert run(source, store, embedder).count("indexed") == 1
        content_hash = store.document_hash("lecture.txt")
        assert store.document_processing_identity("lecture.txt") == "parser-v1"
        assert run(source, store, embedder).count("cached") == 1
        assert embedder.calls == 1
        monkeypatch.setattr(pipeline, "processing_identity", lambda *args: "parser-v2")
        assert run(source, store, embedder).count("indexed") == 1
        assert embedder.calls == 2
        assert store.document_hash("lecture.txt") == content_hash
        assert store.document_processing_identity("lecture.txt") == "parser-v2"
        path.write_text(path.read_text() + " An additional premise is required.")
        assert run(source, store, embedder).count("indexed") == 1
        assert store.document_hash("lecture.txt") != content_hash
        assert embedder.calls == 3


def test_ingest_legacy_refuses_before_work(tmp_path):
    path = tmp_path / "old.db"
    legacy_index(path)
    with (
        VectorStore.open(path, PROFILE) as store,
        pytest.raises(OverfitError, match="read-only"),
    ):
        run(tmp_path / "nonexistent-course", store, FakeEmbedder())


def test_fingerprint_versions_parameters_and_implementation(tmp_path, monkeypatch):
    original = pipeline.processing_identity("pypdf", 100, 10)
    assert pipeline.processing_identity("pypdf", 100, 10) == original
    assert pipeline.processing_identity("pypdf", 101, 10) != original
    assert pipeline.processing_identity("pypdf", 100, 11) != original
    assert pipeline.processing_identity("docling", 100, 10) != original
    monkeypatch.setattr(pipeline.metadata, "version", lambda name: "different-version")
    changed_version = pipeline.processing_identity("pypdf", 100, 10)
    assert changed_version != original
    code = tmp_path / "fake_parser.py"
    code.write_text("original strategy")
    monkeypatch.setattr(
        pipeline, "parser", SimpleNamespace(__name__="parser", __file__=str(code))
    )
    first = pipeline.processing_identity("pypdf", 100, 10)
    code.write_text("new cleaning strategy")
    assert pipeline.processing_identity("pypdf", 100, 10) != first


def test_source_changes_between_cache_hash_and_parser_never_cached(
    tmp_path, monkeypatch
):
    directory = tmp_path / "course"
    directory.mkdir()
    path = directory / "notes.txt"
    path.write_text("The old premise has enough text for an ordinary input chunk. " * 4)
    real_parse = pipeline.parser.parse

    def changing_parse(path, **kwargs):
        path.write_text(
            "The new premise is different and still has sufficient text. " * 4
        )
        return real_parse(path, **kwargs)

    monkeypatch.setattr(pipeline.parser, "parse", changing_parse)
    embedder = FakeEmbedder()
    with VectorStore.open(tmp_path / "index.db", PROFILE, create=True) as store:
        report = run(directory, store, embedder)
        assert report.count("failed") == 1
        assert "Source changed" in report.failed[0].detail
        assert embedder.calls == 0
        assert store.document_hash("notes.txt") is None
        assert store.stats()["chunks"] == 0
