"""Layer 5 -- Vector Store.

One SQLite file per course, holding everything Ingestion produces and
everything Query consumes. The public surface is deliberately tiny --
`add`, `search`, and a few cache helpers -- because this is the single seam
between the two halves of the pipeline. Keeping it narrow is what lets
either side be rewritten without disturbing the other, and it is the same
shape pgvector, Qdrant and Pinecone expose, so moving off SQLite later means
rewriting this file and nothing else.

Three tables:

* ``chunks``    text and provenance, one row per chunk
* ``vec_chunks``the vectors, in a sqlite-vec virtual table, joined by rowid
* ``documents`` a content hash per source file, so unchanged files are
                skipped on re-ingest
* ``meta``      the settings this index was built with

``meta`` is the important one. Vectors from two different embedding models
are not comparable, but nothing about them *looks* wrong -- so the mismatch
is detected here, loudly, instead of being discovered as mysteriously poor
answers weeks later.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from overfit.errors import IndexMismatchError, IndexMissingError
from overfit.models import Chunk, EmbeddedChunk, RetrievedChunk, Vector

__all__ = ["VectorStore", "IndexProfile", "SCHEMA_VERSION"]


# Bumped whenever the table layout changes in a way that old files cannot
# satisfy. Checked alongside the model settings on open.
SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class IndexProfile:
    """The settings an index was built with.

    Everything here changes the meaning of the stored vectors or the text
    they describe, so a mismatch makes the index invalid rather than merely
    stale. Note what is *absent*: `top_k` and anything about generation,
    because those are decided per query and leave the index untouched.

    `parser` is here for a failure that is otherwise completely silent.
    Switching PDF backend changes the extracted text, and therefore every
    chunk and every vector -- but it does not change a single source file,
    so the content-hash cache reports all of them unchanged and a re-ingest
    does nothing at all. Without this field the user gets the old index and
    no indication of it. With it, the store refuses to open and says to
    rebuild. Only the backend name is recorded, not a version: which method
    was used is the question worth answering here.
    """

    embed_model: str
    embed_dim: int
    chunk_size: int
    chunk_overlap: int
    parser: str = "pypdf"

    def as_meta(self) -> dict[str, str]:
        return {
            "schema_version": SCHEMA_VERSION,
            "embed_model": self.embed_model,
            "embed_dim": str(self.embed_dim),
            "chunk_size": str(self.chunk_size),
            "chunk_overlap": str(self.chunk_overlap),
            "parser": self.parser,
        }


class VectorStore:
    """A course's index. Use as a context manager."""

    def __init__(self, connection: sqlite3.Connection, profile: IndexProfile) -> None:
        self._db = connection
        self._profile = profile

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path,
        profile: IndexProfile,
        *,
        course: str = "",
        create: bool = False,
    ) -> VectorStore:
        """Open an index, verifying it was built the same way we would build it.

        Args:
            create: build the file if missing. Ingestion passes True; every
                read path passes False so that querying a course nobody has
                ingested says so plainly.
        """
        if not path.exists():
            if not create:
                raise IndexMissingError(course or path.stem, path)
            path.parent.mkdir(parents=True, exist_ok=True)

        connection = _connect(path)
        store = cls(connection, profile)
        store._create_schema()
        store._verify(course or path.stem)
        return store

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schema ------------------------------------------------------------

    def _create_schema(self) -> None:
        dim = self._profile.embed_dim
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    rowid    INTEGER PRIMARY KEY,
                    id       TEXT UNIQUE NOT NULL,
                    text     TEXT NOT NULL,
                    source   TEXT NOT NULL,
                    page     INTEGER NOT NULL,
                    page_end INTEGER,
                    section  TEXT
                );
                CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source);

                CREATE TABLE IF NOT EXISTS documents (
                    source      TEXT PRIMARY KEY,
                    hash        TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    indexed_at  TEXT NOT NULL
                );
                """
            )
            # The vector table cannot be created with IF NOT EXISTS in older
            # sqlite-vec builds, so check first. Its width is fixed at
            # creation, which is precisely why the dimension is probed from
            # the model before we ever get here.
            existing = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'vec_chunks'"
            ).fetchone()
            if not existing:
                self._db.execute(
                    f"CREATE VIRTUAL TABLE vec_chunks USING vec0("
                    f"embedding float[{dim}])"
                )

            if not self._db.execute("SELECT 1 FROM meta").fetchone():
                self._db.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    self._profile.as_meta().items(),
                )

    def _verify(self, course: str) -> None:
        """Refuse to use an index built with incompatible settings."""
        stored = dict(self._db.execute("SELECT key, value FROM meta").fetchall())
        for key, current in self._profile.as_meta().items():
            if stored.get(key, current) != current:
                raise IndexMismatchError(key, stored[key], current, course)

    # -- writing -----------------------------------------------------------

    def add(self, items: list[EmbeddedChunk]) -> None:
        """Insert chunks and their vectors in a single transaction.

        Idempotent on chunk id: re-running an ingest that failed halfway
        repairs the index rather than duplicating it. That is only true
        because chunk ids are deterministic, which is why `Chunk.make_id`
        derives them from the source and page instead of a counter or a uuid.
        """
        if not items:
            return

        import sqlite_vec

        with self._db:
            for item in items:
                chunk = item.chunk
                if len(item.embedding) != self._profile.embed_dim:
                    raise ValueError(
                        f"chunk {chunk.id} has {len(item.embedding)} dimensions, "
                        f"index expects {self._profile.embed_dim}"
                    )

                # Replace any previous version of this chunk, vector included,
                # so the two tables can never drift apart.
                previous = self._db.execute(
                    "SELECT rowid FROM chunks WHERE id = ?", (chunk.id,)
                ).fetchone()
                if previous:
                    self._db.execute(
                        "DELETE FROM vec_chunks WHERE rowid = ?", (previous[0],)
                    )
                    self._db.execute("DELETE FROM chunks WHERE rowid = ?", (previous[0],))

                cursor = self._db.execute(
                    "INSERT INTO chunks(id, text, source, page, page_end, section) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        chunk.id,
                        chunk.text,
                        chunk.source,
                        chunk.page,
                        chunk.page_end,
                        chunk.section,
                    ),
                )
                self._db.execute(
                    "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                    (cursor.lastrowid, sqlite_vec.serialize_float32(item.embedding)),
                )

    def remove_document(self, source: str) -> int:
        """Drop every chunk from one file. Used before re-indexing a change."""
        with self._db:
            rows = self._db.execute(
                "SELECT rowid FROM chunks WHERE source = ?", (source,)
            ).fetchall()
            for (rowid,) in rows:
                self._db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rowid,))
            self._db.execute("DELETE FROM chunks WHERE source = ?", (source,))
            self._db.execute("DELETE FROM documents WHERE source = ?", (source,))
        return len(rows)

    # -- reading -----------------------------------------------------------

    def search(
        self,
        query_vector: Vector,
        top_k: int,
        *,
        fetch_k: int | None = None,
    ) -> list[RetrievedChunk]:
        """Return the `top_k` nearest chunks.

        Args:
            fetch_k: how many candidates to pull from the vector index before
                narrowing to `top_k`. Today they are the same; the parameter
                exists so that adding a re-ranker later -- which needs a wider
                candidate pool to reorder -- does not change this signature
                or any caller.

        Scores are cosine similarity in [0, 1]. Because vectors are stored
        normalised, squared L2 distance and cosine carry the same ordering,
        and the conversion below is exact rather than an approximation.
        """
        import sqlite_vec

        candidates = max(fetch_k or top_k, top_k)
        rows = self._db.execute(
            """
            SELECT c.id, c.text, c.source, c.page, c.page_end, c.section, v.distance
            FROM vec_chunks v
            JOIN chunks c ON c.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (sqlite_vec.serialize_float32(query_vector), candidates),
        ).fetchall()

        results = [
            RetrievedChunk(
                chunk=Chunk(
                    id=row[0],
                    text=row[1],
                    source=row[2],
                    page=row[3],
                    page_end=row[4],
                    section=row[5],
                ),
                # ||a-b||^2 = 2 - 2cos for unit vectors.
                score=1.0 - (row[6] ** 2) / 2.0,
            )
            for row in rows
        ]
        return results[:top_k]

    # -- cache -------------------------------------------------------------

    def document_hash(self, source: str) -> str | None:
        """The content hash recorded when this file was last indexed."""
        row = self._db.execute(
            "SELECT hash FROM documents WHERE source = ?", (source,)
        ).fetchone()
        return row[0] if row else None

    def record_document(self, source: str, content_hash: str, chunk_count: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO documents(source, hash, chunk_count, indexed_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    source,
                    content_hash,
                    chunk_count,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, object]:
        chunks = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        vectors = self._db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        documents = self._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        meta = dict(self._db.execute("SELECT key, value FROM meta").fetchall())
        return {
            "chunks": chunks,
            "vectors": vectors,
            "documents": documents,
            "consistent": chunks == vectors,
            **meta,
        }

    def all_embedded(self) -> list[EmbeddedChunk]:
        """Every chunk with its vector.

        Needed by coverage sampling, which is not a search: choosing material
        that spans a whole course is a question about the shape of the corpus,
        not about similarity to any query. At a few thousand chunks reading
        them all is trivially cheap; past that this would want to become a
        clustering step performed once at ingest time.
        """
        import struct

        rows = self._db.execute(
            """
            SELECT c.id, c.text, c.source, c.page, c.page_end, c.section, v.embedding
            FROM chunks c
            JOIN vec_chunks v ON v.rowid = c.rowid
            ORDER BY c.rowid
            """
        ).fetchall()

        dim = self._profile.embed_dim
        return [
            EmbeddedChunk(
                chunk=Chunk(
                    id=row[0],
                    text=row[1],
                    source=row[2],
                    page=row[3],
                    page_end=row[4],
                    section=row[5],
                ),
                embedding=list(struct.unpack(f"{dim}f", row[6])),
            )
            for row in rows
        ]

    def sources(self) -> list[tuple[str, int]]:
        return list(
            self._db.execute(
                "SELECT source, COUNT(*) FROM chunks GROUP BY source ORDER BY source"
            ).fetchall()
        )


def _connect(path: Path) -> sqlite3.Connection:
    """Open SQLite with the vector extension loaded."""
    import sqlite_vec

    connection = sqlite3.connect(path)
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    # Write-ahead logging keeps a long ingest from blocking a concurrent read,
    # and survives an interrupted run more gracefully than the default.
    connection.execute("PRAGMA journal_mode = WAL")
    return connection
