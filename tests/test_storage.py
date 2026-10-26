"""Local SQLite roundtrips and read-only historical compatibility; fake vectors."""

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest
import sqlite_vec

from overfit.errors import IndexMismatchError, OverfitError
from overfit.models import Chunk, EmbeddedChunk
from overfit.query.selection import spread
from overfit.storage.store import IndexProfile, VectorStore

PROFILE = IndexProfile("fake", 2, 100, 10)


def sample_chunk():
    text = "The preceding condition does not imply the converse. " * 5
    return Chunk(
        "n_p5_c0",
        text,
        "notes.pdf",
        5,
        6,
        provenance={
            "schema_version": 1,
            "document_sha256": "d" * 64,
            "page_count": 12,
            "extractor": "test/1",
            "blocks": [
                {
                    "block_id": "paragraph/0",
                    "label": "text",
                    "parent_id": None,
                    "context_id": None,
                    "text": text,
                    "locations": [
                        {"page": 5, "charspan": [0, len(text)], "bbox": None},
                        {"page": 6, "charspan": None, "bbox": None},
                    ],
                    "text_span": [0, len(text)],
                    "block_span": [0, len(text)],
                    "mapping": "exact",
                }
            ],
        },
    )


def test_storage_provenance_roundtrip_selection_and_cache(tmp_path):
    path = tmp_path / "new.db"
    chunk = sample_chunk()
    with VectorStore.open(path, PROFILE, create=True) as store:
        store.add([EmbeddedChunk(chunk, [1.0, 0.0])])
        store.record_document(
            "notes.pdf", "sourcehash", 1, processing_identity="strategy"
        )
    with VectorStore.open(path, PROFILE) as store:
        assert store.stats()["schema_version"] == "2"
        assert store.search([1.0, 0.0], 1)[0].chunk == chunk
        selected = spread(store.all_embedded(), 1)[0].chunk
        assert selected == chunk
        assert store.document_hash("notes.pdf") == "sourcehash"
        assert store.document_processing_identity("notes.pdf") == "strategy"
        # No shared metadata dictionary leaks back into storage.
        selected.provenance["page_count"] = 999
        assert store.all_embedded()[0].chunk.provenance["page_count"] == 12


def test_storage_revalidates_mutated_metadata_and_rolls_back(tmp_path):
    chunk = sample_chunk()
    with VectorStore.open(tmp_path / "new.db", PROFILE, create=True) as store:
        store.add([EmbeddedChunk(chunk, [1.0, 0.0])])
        chunk.provenance["blocks"][0]["text_span"] = [0, 99999]
        with pytest.raises(ValueError):
            store.add([EmbeddedChunk(chunk, [0.0, 1.0])])
        assert store.stats()["chunks"] == 1
        assert store.stats()["consistent"]
        assert (
            store.all_embedded()[0].chunk.provenance["blocks"][0]["text_span"][1]
            < 99999
        )


def test_storage_revalidates_persisted_metadata(tmp_path):
    path = tmp_path / "new.db"
    with VectorStore.open(path, PROFILE, create=True) as store:
        store.add([EmbeddedChunk(sample_chunk(), [1.0, 0.0])])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE chunks SET provenance = ?", (json.dumps({"surprise": 1}),))
    db.close()
    with VectorStore.open(path, PROFILE) as store:
        with pytest.raises(ValueError):
            store.all_embedded()
        with pytest.raises(ValueError):
            store.search([1.0, 0.0], 1)


def legacy_index(path):
    """Create an actual schema-1 layout, not schema 2 with a changed label."""
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE chunks(rowid INTEGER PRIMARY KEY,id TEXT UNIQUE,text TEXT,
                            source TEXT,page INTEGER,page_end INTEGER,section TEXT);
        CREATE TABLE documents(source TEXT PRIMARY KEY,hash TEXT,chunk_count INTEGER,
                               indexed_at TEXT);
        CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[2]);
    """)
    meta = {**PROFILE.as_meta(), "schema_version": "1"}
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    db.execute(
        "INSERT INTO chunks VALUES (1, 'old', 'old source text', 'n.pdf', 2, NULL, NULL)"
    )
    db.execute("INSERT INTO documents VALUES ('n.pdf', 'hash', 1, 'date')")
    db.execute(
        "INSERT INTO vec_chunks VALUES (1, ?)",
        (sqlite_vec.serialize_float32([1.0, 0.0]),),
    )
    db.commit()
    db.close()


def test_legacy_index_reads_without_migration_or_byte_change(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_index(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with VectorStore.open(path, PROFILE) as store:
        assert store.all_embedded()[0].chunk.provenance is None
        assert store.search([1.0, 0.0], 1)[0].chunk.provenance is None
        assert store.stats()["schema_version"] == "1"
        assert store.document_processing_identity("n.pdf") is None
        assert store.document_hash("n.pdf") == "hash"
        for operation in [
            lambda: store.add([]),
            lambda: store.remove_document("n.pdf"),
            lambda: store.record_document("n.pdf", "new", 1),
        ]:
            with pytest.raises(OverfitError, match="separate new index"):
                operation()
    with pytest.raises(OverfitError, match="read-only"):
        VectorStore.open(path, PROFILE, create=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["legacy.db"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("embed_model", "other"),
        ("embed_dim", 3),
        ("chunk_size", 120),
        ("chunk_overlap", 20),
        ("parser", "docling"),
    ],
)
def test_profile_checks_still_enforced(tmp_path, legacy, field, value):
    path = tmp_path / "index.db"
    if legacy:
        legacy_index(path)
    else:
        VectorStore.open(path, PROFILE, create=True).close()
    with pytest.raises(IndexMismatchError, match=field):
        VectorStore.open(path, replace(PROFILE, **{field: value}))
