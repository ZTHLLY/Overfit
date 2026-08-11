"""Layer 1 -- Loader.

Finds source files. Deliberately does nothing else: no reading, no parsing.
Keeping discovery separate means that adding Google Drive, a Notion export or
a zip archive later changes this file and no other.

Output: a sorted list of paths. Sorted matters more than it looks -- directory
iteration order is filesystem-dependent, and unstable ordering would make
chunk ids drift between runs on different machines.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from overfit.errors import CourseNotFoundError, NoDocumentsError

__all__ = ["SUPPORTED_EXTENSIONS", "find_documents", "file_hash", "count_ignored"]


# Extensions we have a parser for. Kept here rather than in config because
# it is a property of the code, not of the user's environment.
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".pdf", ".md", ".markdown", ".txt")

# Directory names that are never course material. Cloud sync tools and
# archives leave these behind, and they can contain copies that would be
# indexed twice.
_SKIP_DIRS = {"__MACOSX", "node_modules", ".git", ".obsidian", "__pycache__"}


def find_documents(
    directory: Path,
    extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS,
) -> list[Path]:
    """Collect every supported document under `directory`, recursively.

    Recursion is on by default because real course folders are rarely flat --
    people organise by week, by topic, or by whatever the unit site handed
    them as a zip.

    Raises:
        CourseNotFoundError: the directory does not exist or is a file.
        NoDocumentsError: it exists but holds nothing readable.
    """
    if not directory.is_dir():
        raise CourseNotFoundError(directory)

    wanted = {ext.lower() for ext in extensions}
    found: list[Path] = []

    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in wanted:
            continue
        if _is_hidden_or_skipped(path, directory):
            continue
        found.append(path)

    if not found:
        raise NoDocumentsError(directory, extensions)

    # Sort by the path relative to the root so results do not depend on where
    # the course folder happens to live.
    return sorted(found, key=lambda p: p.relative_to(directory).as_posix().lower())


def count_ignored(
    directory: Path,
    extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS,
) -> dict[str, int]:
    """Files present but excluded by the configured extensions.

    Filtering by extension is useful -- it is how a folder holding both a PDF
    and a Markdown copy of the same lecture avoids being indexed twice -- but
    a filter that works in silence is a trap. Someone adds notes.md next
    semester, never sees it in an exam, and has nothing anywhere to explain
    why. Reporting what was passed over costs one line of output and removes
    an entire category of confusion.

    Junk the loader would never index in any configuration (dotfiles, macOS
    resource forks) is not reported: mentioning it would be noise, not news.
    """
    if not directory.is_dir():
        return {}

    wanted = {ext.lower() for ext in extensions}
    ignored: dict[str, int] = {}
    for path in directory.rglob("*"):
        if not path.is_file() or _is_hidden_or_skipped(path, directory):
            continue
        suffix = path.suffix.lower()
        if suffix and suffix not in wanted:
            ignored[suffix] = ignored.get(suffix, 0) + 1
    return ignored


def _is_hidden_or_skipped(path: Path, root: Path) -> bool:
    """Filter out dotfiles, dot-directories and known junk folders.

    macOS in particular scatters `._foo.pdf` resource forks and `.DS_Store`
    around; picking those up produces documents that parse into garbage.
    """
    for part in path.relative_to(root).parts:
        if part.startswith(".") or part in _SKIP_DIRS:
            return True
    return False


def file_hash(path: Path, _chunk: int = 1 << 20) -> str:
    """Content hash of a file, used as the cache key for ingestion.

    Hashing the *content* rather than the name or mtime means renaming a file,
    touching it, or re-exporting it byte-identically costs nothing, while
    editing a single sentence rebuilds exactly that one file. Since embedding
    is the only expensive step in the pipeline, this is where the project's
    running cost is actually decided.

    Read in blocks so a large PDF never has to sit in memory whole.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_chunk):
            digest.update(block)
    return digest.hexdigest()
