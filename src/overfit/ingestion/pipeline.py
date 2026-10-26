"""Ingestion orchestration -- runs layers 1 to 4 and writes into layer 5.

This module owns no logic of its own. It decides *what runs when*: which
files can be skipped, what happens when one of them fails, and what gets
reported. Keeping that separate from the layers means each layer stays a
pure function of its input and can be tested without a database.

Two behaviours matter more than they look:

**Caching by source, content hash and processing identity.** Embedding is the only step that costs
real time, so unchanged content under the same processing strategy is skipped.
Dependency/code/parameter changes invalidate that cache. A rename creates
a new source, and sources removed from disk remain until a full rebuild.

**Parser failure isolation.** A scanned or unreadable PDF in a folder of thirty
does not abort the other twenty-nine; those failures are collected and
reported. Hashing, embedding and database errors occur outside that per-file
catch and still abort the run, because continuing after those failures could
leave the index state ambiguous.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from overfit import models, provenance
from overfit.embedding import Embedder
from overfit.errors import OverfitError
from overfit.ingestion import chunker, loader, parser
from overfit.models import EmbeddedChunk
from overfit.storage.store import VectorStore

__all__ = ["FileOutcome", "IngestReport", "ingest"]


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
        force: re-embed even when both content and processing identity match.
            Parser/cleaner/chunker code and dependency changes already invalidate
            the cache automatically.
    """
    store.require_writable()
    identity = processing_identity(pdf_backend, chunk_size, chunk_overlap)
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

        if (
            not force
            and store.document_hash(source) == content_hash
            and store.document_processing_identity(source) == identity
        ):
            finish(FileOutcome(source, "cached"))
            continue

        try:
            document = parser.parse(path, source=source, backend=pdf_backend)
        except OverfitError as exc:
            finish(FileOutcome(source, "failed", detail=str(exc)))
            continue

        parsed_hash = (document.provenance or {}).get("document_sha256")
        if parsed_hash is not None and parsed_hash != content_hash:
            finish(
                FileOutcome(
                    source,
                    "failed",
                    detail="Source changed before parsing; retry ingestion",
                )
            )
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
        store.record_document(
            source, content_hash, len(chunks), processing_identity=identity
        )

        report.chunks_added += len(chunks)
        report.embeddings_computed += len(vectors)
        finish(FileOutcome(source, "indexed", chunks=len(chunks)))

    report.seconds = time.monotonic() - started
    return report


def processing_identity(backend: str, chunk_size: int, chunk_overlap: int) -> str:
    """Deterministic local-only cache key; no converter or model is loaded.

    Content identity remains separate in documents.hash. Hashing installed
    source also catches cleaner/provenance edits during editable development;
    this avoids a stale cache when a version string was not manually bumped.
    """
    packages = ["pypdf"]
    if backend in {"docling", "docling+formula"}:
        packages += [
            "docling-slim",
            "docling-core",
            "docling-parse",
            "docling-ibm-models",
            "pypdfium2",
            "torch",
            "transformers",
        ]
    versions = {}
    for package in packages:
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = None
    implementations = {
        module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for module in (parser, chunker, provenance, models)
    }
    payload = {
        "schema_version": 1,
        "backend": backend,
        "packages": versions,
        "implementation": implementations,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
