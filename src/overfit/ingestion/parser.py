"""Layer 2 -- Parser.

Turns a file into text that still knows which page it came from. This is
where traceability is born: no later layer can reconstruct a page number, so
if it is dropped here the citation feature dies quietly.

It is also where cleaning belongs. A PDF is a *print* format, not a semantic
one -- it records where ink goes, not what belongs together. Extraction
therefore yields running headers, footers, page numbers and words split
across line breaks. Left in, that noise ends up inside the embeddings, and
the resulting retrieval failures look like a chunking or prompting problem
when they are neither.

The parser is written as a dispatch table on purpose. Course material comes
in whatever shape a lecturer felt like exporting, so the ability to slot in a
better backend (pdfplumber for tables, OCR for scans) without touching any
other layer is a requirement, not a nicety.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Callable

from overfit.errors import EmptyExtractionError, ExtractionError, UnsupportedFormatError
from overfit.models import Page, ParsedDocument

__all__ = ["parse", "clean_page"]


# A page holding fewer characters than this is treated as blank. Section
# dividers and title slides legitimately land here, so it is only used to
# judge a document as a whole, never to drop an individual page.
_MIN_MEANINGFUL_CHARS = 20

# A line repeated on at least this fraction of pages is running furniture
# (unit code, lecturer name, copyright notice) rather than content.
_HEADER_REPEAT_RATIO = 0.6

# Below this many pages the statistics are meaningless -- a 3-page handout may
# legitimately repeat a phrase at the top of every page.
_MIN_PAGES_FOR_HEADER_DETECTION = 4


def parse(path: Path) -> ParsedDocument:
    """Read one file into a ParsedDocument.

    Raises:
        UnsupportedFormatError: no backend claims this extension.
        ExtractionError: the backend failed on this file.
        EmptyExtractionError: extraction produced essentially nothing,
            which almost always means a scanned PDF.
    """
    backend = _BACKENDS.get(path.suffix.lower())
    if backend is None:
        raise UnsupportedFormatError(path)

    pages = backend(path)
    pages = _strip_running_lines(pages)
    pages = [Page(number=p.number, text=drop_figure_debris(p.text)) for p in pages]
    pages = [Page(number=p.number, text=clean_page(p.text)) for p in pages]

    document = ParsedDocument(source=path.name, pages=pages)

    # Fail loudly rather than letting a blank document into the index. A
    # capability may be missing; it must not be faked.
    if document.is_empty:
        raise EmptyExtractionError(path)

    return document


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _parse_pdf(path: Path) -> list[Page]:
    """Extract text page by page with pypdf.

    Page numbers are 1-based so they match what a human sees in a reader --
    the citation is useless if the reader has to mentally add one.
    """
    import logging

    from pypdf import PdfReader  # imported lazily: keeps `--help` fast

    # pypdf narrates malformed files straight to stderr ("invalid pdf
    # header", "EOF marker not found"). We already convert those conditions
    # into ExtractionError with a message the user can act on, so the raw
    # commentary only obscures our own output.
    logging.getLogger("pypdf").setLevel(logging.ERROR)

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # Many university PDFs carry an empty owner password, which pypdf
            # can open; a real password cannot be guessed and must fail.
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001 - surfaced as ExtractionError
                raise ExtractionError(path, f"encrypted PDF ({exc})") from exc
        return [
            Page(number=index, text=page.extract_text() or "")
            for index, page in enumerate(reader.pages, start=1)
        ]
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises a wide variety
        raise ExtractionError(path, f"{type(exc).__name__}: {exc}") from exc


def _parse_text(path: Path) -> list[Page]:
    """Read Markdown or plain text as a single page.

    Text files have no pages, so citations degrade to the file name alone.
    That is honest: inventing page boundaries would produce references a
    reader cannot follow.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExtractionError(path, str(exc)) from exc
    return [Page(number=1, text=content)]


_BACKENDS: dict[str, Callable[[Path], list[Page]]] = {
    ".pdf": _parse_pdf,
    ".md": _parse_text,
    ".markdown": _parse_text,
    ".txt": _parse_text,
}


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

_HYPHEN_BREAK = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
_MANY_SPACES = re.compile(r"[ \t]{2,}")


def clean_page(text: str) -> str:
    """Normalise the text of a single page.

    Three fixes, in order:

    1. Re-join words split across a line break ("over-\\nfitting"). Left
       alone these become two meaningless fragments that damage the embedding
       and, worse, make the chunk unsearchable for the very word it contains.
    2. Collapse runs of spaces. Column-based layouts produce long stretches of
       padding that carry no meaning but do consume the chunk budget.
    3. Collapse excess blank lines while keeping paragraph breaks, which are
       the only structural signal the chunker gets.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _MANY_SPACES.sub(" ", text)
    text = _TRAILING_SPACE.sub("", text)
    text = _MANY_BLANK_LINES.sub("\n\n", text)
    return text.strip()


# A figure's labels arrive as a run of short, unpunctuated fragments. One
# such line is unremarkable -- slide titles look identical -- so only a run
# of them is treated as debris.
_DEBRIS_RUN = 6
_DEBRIS_MAX_CHARS = 32
_BULLET_LINE = re.compile(r"^\s*([•●▪–—\-*·]|\d+[.)]|[a-z][.)])\s")


def drop_figure_debris(text: str) -> str:
    """Remove scattered text belonging to charts, diagrams and tables.

    Extraction flattens a figure into whatever order its labels happen to sit
    in the file, producing passages like::

        jaguar
        dalmatia n
        elderberry
        currant
        grape

    That is not content. Embedded, it becomes a vector with no coherent
    meaning: it will never satisfy a real question, yet it occupies a slot in
    every top-k it happens to land in. Worse, because such lines carry no
    punctuation and arbitrary capitalisation, they also fool the page-
    continuation test in the chunker into merging slides that should stay
    apart -- so removing them fixes two problems at once.

    Detection is deliberately conservative. A line only counts as debris in
    the company of several neighbours like it, and bullets are exempt: a
    slide's bullet list is short and unpunctuated too, but it is real prose.
    """
    lines = text.splitlines()
    flags = [_is_fragment(line) for line in lines]

    keep = [True] * len(lines)
    run_start = 0
    for index in range(len(lines) + 1):
        if index < len(lines) and flags[index]:
            continue
        if index - run_start >= _DEBRIS_RUN:
            for position in range(run_start, index):
                keep[position] = False
        run_start = index + 1

    return "\n".join(line for line, wanted in zip(lines, keep) if wanted)


def _is_fragment(line: str) -> bool:
    """A line that could plausibly be a stray label rather than a sentence."""
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) > _DEBRIS_MAX_CHARS:
        return False
    if _BULLET_LINE.match(line):
        return False  # a real bullet, however terse
    if stripped[-1] in ".!?:":
        return False  # finished sentences are content
    return True


def _strip_running_lines(pages: list[Page]) -> list[Page]:
    """Remove headers and footers that repeat across the document.

    Detected statistically rather than by rule: any first or last line that
    shows up on most pages is furniture. This catches unit codes, lecturer
    names and copyright notices without needing to know anything about the
    specific document -- which matters, because course material comes from
    dozens of different templates.

    Bare page numbers are dropped too. They vary per page so the frequency
    test never sees them, yet they are pure noise inside a chunk.
    """
    if len(pages) < _MIN_PAGES_FOR_HEADER_DETECTION:
        return pages

    counts: Counter[str] = Counter()
    for page in pages:
        lines = [line.strip() for line in page.text.splitlines() if line.strip()]
        if not lines:
            continue
        # Only the outer edges of a page can be furniture; a repeated line in
        # the middle is far more likely to be a genuine recurring definition.
        counts.update({lines[0], lines[-1]})

    threshold = len(pages) * _HEADER_REPEAT_RATIO
    furniture = {line for line, count in counts.items() if count >= threshold}

    cleaned: list[Page] = []
    for page in pages:
        kept = [
            line
            for line in page.text.splitlines()
            if line.strip() not in furniture and not _is_bare_page_number(line)
        ]
        cleaned.append(Page(number=page.number, text="\n".join(kept)))
    return cleaned


_BARE_NUMBER = re.compile(r"^\s*(page\s*)?\d{1,4}\s*(/\s*\d{1,4})?\s*$", re.IGNORECASE)


def _is_bare_page_number(line: str) -> bool:
    """True for lines like "12", "Page 3" or "4 / 20"."""
    return bool(_BARE_NUMBER.match(line))


def is_probably_scanned(document: ParsedDocument) -> bool:
    """Heuristic: most pages carry almost no text.

    Used for warnings rather than control flow -- a deck of image-heavy slides
    can look like this and still be perfectly usable.
    """
    if not document.pages:
        return True
    sparse = sum(1 for p in document.pages if len(p.text) < _MIN_MEANINGFUL_CHARS)
    return sparse >= len(document.pages) * 0.8
