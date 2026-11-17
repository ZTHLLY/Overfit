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
    "CourseNotFoundError",
    "EmbeddingError",
    "EmptyExtractionError",
    "ExtractionError",
    "IndexMismatchError",
    "IndexMissingError",
    "MissingApiKeyError",
    "MissingSourcesError",
    "NoDocumentsError",
    "OverfitError",
    "RerankConfigurationError",
    "RerankContractError",
    "RerankError",
    "RerankExternalError",
    "RerankLogError",
    "UnsupportedFormatError",
]


def _ingest_command(course: str) -> str:
    """The ingest command to suggest, matching how the user is running."""
    from overfit.config import get_settings

    try:
        here = getattr(get_settings(), "course_root", None) is not None
    except (OSError, ValueError):  # an error message must never fail to build
        here = False
    return "overfit ingest" if here else f"overfit ingest --course {course}"


class OverfitError(Exception):
    """Base class for every error this tool raises on purpose."""


class RerankError(OverfitError):
    """A rerank run could not produce a complete, recorded result."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        failure_category: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.failure_category = failure_category


class RerankExternalError(RerankError):
    """No successful HTTP response was received; bounded retry is allowed."""

    def __init__(self, message: str = "Reranker request did not succeed.", *, status: int | None = None, request_id: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message, failure_category="external_or_timeout")
        self.status = status
        self.request_id = request_id
        self.retry_after = retry_after


class RerankContractError(RerankError):
    """A local input or successful HTTP response violated the frozen contract."""


class RerankConfigurationError(RerankContractError):
    """A query path lacks valid reranker configuration."""


class RerankLogError(RerankContractError):
    """The mandatory rerank audit record could not be persisted."""


class MissingApiKeyError(OverfitError):
    """A hosted model endpoint is configured but its key is not."""

    def __init__(self, variable: str) -> None:
        super().__init__(
            f"{variable} is not set. Add it to ~/.config/overfit/.env, "
            f"e.g.\n    {variable}=sk-..."
        )
        self.variable = variable


class MissingSourcesError(OverfitError):
    """Indexed files are no longer in the course folder.

    Incremental ingestion only looks at files that exist, so a deleted or
    renamed lecture would otherwise stay in the index: its chunks keep being
    drawn into exams, cite a file that is gone, and a rename doubles the
    content. Rather than guess which case it is, stop before any work and
    leave the index untouched.
    """

    _SHOWN = 5

    def __init__(self, missing: list[str] | tuple[str, ...], course: str = "") -> None:
        self.missing = tuple(sorted(missing))
        shown = "\n".join(f"    {source}" for source in self.missing[: self._SHOWN])
        more = len(self.missing) - self._SHOWN
        if more > 0:
            shown += f"\n    ... and {more} more"
        command = _ingest_command(course) if course else "overfit ingest"
        super().__init__(
            f"{len(self.missing)} indexed file(s) are no longer in the course folder:\n"
            f"{shown}\n"
            "They were removed, renamed, or are now excluded by EXTENSIONS. "
            "Nothing was indexed or generated, and the index was not changed.\n"
            f"Restore the files, or rebuild the index:\n    {command} --rebuild"
        )


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
            f"No index for {course!r} at {path}\nRun: {_ingest_command(course)}"
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
            f"    {_ingest_command(course)} --rebuild"
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
