"""Ingestion orchestration -- runs layers 1 to 4 and writes into layer 5.

This module owns no logic of its own. It decides *what runs when*: which
files can be skipped, what happens when one of them fails, and what gets
reported. Keeping that separate from the layers means each layer stays a
pure function of its input and can be tested without a database.

Two behaviours matter more than they look:

**Caching by content hash.** Embedding is the only step that costs real
time, so an unchanged file must cost nothing. Hashing content rather than
names or timestamps means renaming, touching or re-exporting a file is free,
while editing one sentence rebuilds exactly that file.

**Per-file failure isolation.** A scanned PDF in a folder of thirty must not
abort the other twenty-nine. Failures are collected and reported at the end,
so the user learns everything wrong in one run instead of one per attempt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from overfit.embedding import Embedder
from overfit.errors import OverfitError
from overfit.ingestion import chunker, loader, parser
from overfit.models import EmbeddedChunk
from overfit.storage.store import VectorStore

__all__ = ["ingest", "IngestReport", "FileOutcome"]


@dataclass(frozen=True, slots=True)
class FileOutcome:
    """What happened to one source file."""

    name: str
    status: str  # "indexed" | "cached" | "failed"
    chunks: int = 0
    detail: str = ""


@dataclass
class IngestReport:
    outcomes: list[FileOutcome] = field(default_factory=list)
    chunks_added: int = 0
    embeddings_computed: int = 0
    seconds: float = 0.0

    def count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == status)

    @property
    def failed(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]


def ingest(
    directory: Path,
    store: VectorStore,
    embedder: Embedder,
    *,
    chunk_size: int,
    chunk_overlap: int,
    extensions: tuple[str, ...] | None = None,
    pdf_backend: str = "pypdf",
    force: bool = False,
    on_file: Callable[[int, int, Path], None] | None = None,
    on_result: Callable[[FileOutcome], None] | None = None,
    on_embed: Callable[[int, int], None] | None = None,
) -> IngestReport:
    """Index every document under `directory`.

    Args:
        force: re-embed even files whose content is unchanged. Needed after
            changing anything that alters chunk text but is not covered by
            the index profile -- a cleaning rule, say.
    """
    started = time.monotonic()
    report = IngestReport()
    files = loader.find_documents(directory, extensions or loader.SUPPORTED_EXTENSIONS)

    def finish(outcome: FileOutcome) -> None:
        """Record and announce one file, immediately.

        Reporting as we go rather than in a batch at the end matters for a
        long ingest: the user needs to see which file is slow, and which one
        was being read when something went wrong.
        """
        report.outcomes.append(outcome)
        if on_result:
            on_result(outcome)

    for position, path in enumerate(files, start=1):
        if on_file:
            on_file(position, len(files), path)

        # Identify a document by its path relative to the course root, not by
        # its file name. Weekly folders make repeated names normal, and two
        # files called notes.md are two documents -- treating them as one
        # means the second quietly replaces the first.
        source = path.relative_to(directory).as_posix()
        content_hash = loader.file_hash(path)

        if not force and store.document_hash(source) == content_hash:
            finish(FileOutcome(source, "cached"))
            continue

        try:
            document = parser.parse(path, source=source, backend=pdf_backend)
        except OverfitError as exc:
            finish(FileOutcome(source, "failed", detail=str(exc)))
            continue

        chunks = chunker.chunk_document(document, chunk_size, chunk_overlap)
        if not chunks:
            finish(FileOutcome(source, "failed", detail="produced no usable chunks"))
            continue

        vectors = embedder.embed([chunk.text for chunk in chunks], on_progress=on_embed)

        # Clear the old version first: a file that shrank would otherwise
        # leave orphaned chunks behind, and they would keep being retrieved.
        store.remove_document(source)
        store.add(
            [
                EmbeddedChunk(chunk=chunk, embedding=vector)
                for chunk, vector in zip(chunks, vectors)
            ]
        )
        store.record_document(source, content_hash, len(chunks))

        report.chunks_added += len(chunks)
        report.embeddings_computed += len(vectors)
        finish(FileOutcome(source, "indexed", chunks=len(chunks)))

    report.seconds = time.monotonic() - started
    return report
