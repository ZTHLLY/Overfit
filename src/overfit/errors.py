"""Exceptions shared across layers.

Every failure the user can actually cause has its own type, so the CLI can
print something actionable instead of a traceback. The guiding rule for this
project: a capability may be missing, but it must never be faked. A PDF we
cannot read has to raise, never return an empty document -- silent empties
poison the index and are almost impossible to trace back later.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "OverfitError",
    "CourseNotFoundError",
    "NoDocumentsError",
    "UnsupportedFormatError",
    "ExtractionError",
    "EmptyExtractionError",
    "EmbeddingError",
    "IndexMismatchError",
    "IndexMissingError",
]


class OverfitError(Exception):
    """Base class for every error this tool raises on purpose."""


class CourseNotFoundError(OverfitError):
    """The course directory does not exist."""

    def __init__(self, directory: Path) -> None:
        super().__init__(
            f"Course directory not found: {directory}\n"
            f"Check COURSES_DIR in your .env, or pass --path explicitly."
        )
        self.directory = directory


class NoDocumentsError(OverfitError):
    """The directory exists but holds nothing we can read."""

    def __init__(self, directory: Path, extensions: tuple[str, ...]) -> None:
        super().__init__(
            f"No supported documents in: {directory}\n"
            f"Looked for: {', '.join(extensions)} (searched subdirectories too)."
        )
        self.directory = directory


class UnsupportedFormatError(OverfitError):
    """We were handed a file type no parser claims."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"No parser for {path.suffix or '(no extension)'}: {path.name}")
        self.path = path


class EmbeddingError(OverfitError):
    """The embedding service could not be reached, or returned nonsense."""

    def __init__(self, message: str, *, base_url: str = "", model: str = "") -> None:
        detail = ""
        if base_url:
            detail = (
                f"\nEndpoint: {base_url}  Model: {model}"
                f"\nIf this is Ollama, check it is running (`curl {base_url.rstrip('/v1')}`)"
                f" and that the model is pulled (`ollama pull {model}`)."
            )
        super().__init__(message + detail)


class IndexMissingError(OverfitError):
    """Someone asked to query a course that has not been ingested."""

    def __init__(self, course: str, path: Path) -> None:
        super().__init__(
            f"No index for {course!r} at {path}\nRun: overfit ingest --course {course}"
        )
        self.course = course


class IndexMismatchError(OverfitError):
    """The index was built with settings that no longer match the config.

    This exists because the failure it prevents is invisible. Querying a
    bge-m3 index with vectors from another model raises nothing: the numbers
    are the right shape, the distances compute, results come back. They are
    simply meaningless, and the symptom -- "retrieval quality is poor" --
    points at chunking and prompting, which are innocent. Far better to stop
    dead with an instruction.
    """

    def __init__(self, field: str, stored: str, current: str, course: str) -> None:
        super().__init__(
            f"This index was built with {field}={stored!r}, "
            f"but the current configuration says {current!r}.\n"
            f"Vectors from different settings are not comparable, so the index "
            f"must be rebuilt:\n"
            f"    overfit ingest --course {course} --rebuild"
        )
        self.field = field


class ExtractionError(OverfitError):
    """The file was found and recognised, but reading it failed."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"Could not read {path.name}: {reason}")
        self.path = path


class EmptyExtractionError(ExtractionError):
    """Parsing succeeded mechanically but produced (almost) no text.

    Nearly always a scanned PDF: pages are images with no text layer. We
    refuse to continue rather than let a blank document flow into the index,
    because the resulting failure -- retrieval quietly returning nothing
    useful -- would surface many layers away from its cause.
    """

    def __init__(self, path: Path) -> None:
        ExtractionError.__init__(
            self,
            path,
            "no extractable text. This is usually a scanned PDF, whose pages "
            "are images. OCR is not supported yet.",
        )
