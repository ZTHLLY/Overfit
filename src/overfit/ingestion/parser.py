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
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from overfit.errors import (
    EmptyExtractionError,
    ExtractionError,
    OverfitError,
    UnsupportedFormatError,
)
from overfit.models import Page, ParsedDocument

__all__ = [
    "parse",
    "clean_page",
    "find_running_lines",
    "Removal",
    "CleaningReport",
    "cleaning_report",
]


@dataclass(frozen=True, slots=True)
class Removal:
    """One line the parser threw away, and which rule threw it."""

    page: int
    stage: str  # "furniture" | "page-number" | "figure-debris"
    text: str


@dataclass(frozen=True, slots=True)
class CleaningReport:
    """What cleaning did to one document.

    Exists because every stage in layer 2 is lossy and none of them can be
    proved correct. There is no ground truth for "the right amount of
    cleaning", so the only honest instrument is a record of what was taken
    and the chance for a human to disagree with it. Counts alone cannot do
    that job: a rate tells you that too much was deleted, never that the
    wrong thing was.
    """

    source: str
    pages: int
    chars_before: int
    chars_after: int
    furniture: dict[str, int] = field(default_factory=dict)
    removals: list[Removal] = field(default_factory=list)
    numbers_preserved: bool = True

    @property
    def removed_ratio(self) -> float:
        if not self.chars_before:
            return 0.0
        return 1.0 - self.chars_after / self.chars_before

    def by_stage(self, stage: str) -> list[Removal]:
        return [item for item in self.removals if item.stage == stage]

    def margin(self, line: str) -> float:
        """How comfortably a furniture line cleared the threshold, in [0, 1].

        0.6 is the bar; a line sitting at 0.61 is a coin toss the code
        happened to win, and those are the ones to read first.
        """
        return self.furniture.get(line, 0) / self.pages if self.pages else 0.0


def cleaning_report(
    path: Path, source: str | None = None, backend: str = "pypdf"
) -> CleaningReport:
    """Parse a file while recording everything cleaning removed.

    Read-only and independent of the index: this answers "is layer 2 eating
    my content?" before any decision about chunking or retrieval is worth
    making.

    Takes the same `backend` argument as `parse` and for a pointed reason:
    an audit run against a different reader than the one ingestion uses is
    an audit of something that never happened.
    """
    raw = _read(path, backend)
    removals: list[Removal] = []
    cleaned = _clean(raw, record=removals)

    return CleaningReport(
        source=source or path.name,
        pages=len(raw),
        chars_before=sum(len(page.text) for page in raw),
        chars_after=sum(len(page.text) for page in cleaned),
        furniture=find_running_lines(raw),
        removals=removals,
        # The one thing in this layer that is a hard invariant rather than a
        # judgement call. If it ever fails, every citation downstream is off
        # by an unknown amount while still looking perfectly well-formed.
        numbers_preserved=[p.number for p in raw] == [p.number for p in cleaned],
    )


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


def parse(
    path: Path, source: str | None = None, backend: str = "pypdf"
) -> ParsedDocument:
    """Read one file into a ParsedDocument.

    Args:
        source: the name this document should be cited by. Defaults to the
            file name, but ingestion passes the path relative to the course
            root, because weekly folders make repeated file names normal and
            two documents called `notes.md` must not be treated as one.
        backend: which PDF reader to use. Passed in rather than read from
            config, so this layer stays a pure function of its arguments and
            can be tested without an environment. Ignored for text formats,
            which have only one sensible reader.

    Raises:
        UnsupportedFormatError: no backend claims this extension.
        ExtractionError: the backend failed on this file.
        EmptyExtractionError: extraction produced essentially nothing,
            which almost always means a scanned PDF.
    """
    pages = _clean(_read(path, backend))

    document = ParsedDocument(source=source or path.name, pages=pages)

    # Fail loudly rather than letting a blank document into the index. A
    # capability may be missing; it must not be faked.
    if document.is_empty:
        raise EmptyExtractionError(path)

    return document


def _clean(pages: list[Page], record: list[Removal] | None = None) -> list[Page]:
    """Run the three cleaning stages, optionally recording what they removed.

    `parse` and `cleaning_report` both go through here, and that is the point.
    Cleaning is lossy and irreversible, so an audit of it is only worth
    trusting if it describes the same code that ran -- a second
    implementation written for the report would start accurate and drift.
    """
    # Below a handful of pages the frequency test is meaningless -- a 3-page
    # handout may legitimately repeat a phrase at the top of every page -- so
    # the whole stage is skipped, page numbers included.
    if len(pages) < _MIN_PAGES_FOR_HEADER_DETECTION:
        stripped = pages
    else:
        furniture = find_running_lines(pages)
        stripped = []
        for page in pages:
            kept: list[str] = []
            for line in page.text.splitlines():
                if line.strip() and line.strip() in furniture:
                    _note(record, page.number, "furniture", line)
                elif _is_bare_page_number(line):
                    _note(record, page.number, "page-number", line)
                else:
                    kept.append(line)
            stripped.append(Page(number=page.number, text="\n".join(kept)))

    debrided: list[Page] = []
    for page in stripped:
        lines = page.text.splitlines()
        keep = _debris_mask(lines)
        for line, wanted in zip(lines, keep):
            if not wanted:
                _note(record, page.number, "figure-debris", line)
        debrided.append(Page(number=page.number, text="\n".join(
            line for line, wanted in zip(lines, keep) if wanted
        )))

    return [Page(number=p.number, text=clean_page(p.text)) for p in debrided]


def _note(record: list[Removal] | None, page: int, stage: str, line: str) -> None:
    if record is not None and line.strip():
        record.append(Removal(page=page, stage=stage, text=line.strip()))


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


def _parse_docling(path: Path, *, formulas: bool) -> list[Page]:
    """Extract text with Docling's layout model, page by page.

    Why bother, given pypdf already works: pypdf reads a PDF as a stream of
    positioned glyphs, so a table arrives as a heap of loose cells and a
    fraction arrives as two unrelated lines. Docling runs layout and table
    models over each page, which recovers the structures that were only ever
    visual -- and the cleaning stage downstream stops mistaking them for
    debris, because a markdown table row is long and punctuated where a
    stray cell was short and bare.

    The output contract is deliberately identical to `_parse_pdf`: a page
    per page, 1-based, no gaps. Anything richer would have to be carried by
    `Page`, and widening that type is a separate decision from swapping the
    reader.

    Empty pages are preserved rather than skipped. Page numbers are the one
    thing this layer cannot regenerate, so the list index must keep matching
    what a human sees in a reader even when a page yields nothing.
    """
    converter = _docling_converter(formulas)

    try:
        document = converter.convert(str(path)).document
    except Exception as exc:  # noqa: BLE001 - docling raises a wide variety
        raise ExtractionError(path, f"{type(exc).__name__}: {exc}") from exc

    buckets: dict[int, list[str]] = {}
    highest = 0
    for item, _level in document.iterate_items():
        number = _docling_page_of(item)
        if number is None:
            continue
        highest = max(highest, number)
        text = _docling_text_of(item, document)
        if text:
            buckets.setdefault(number, []).append(text)

    total = _docling_page_count(document) or highest
    return [
        Page(number=number, text="\n\n".join(buckets.get(number, [])))
        for number in range(1, total + 1)
    ]


def _docling_page_of(item: object) -> int | None:
    """The 1-based page an item sits on, or None if it claims no position."""
    provenance = getattr(item, "prov", None) or []
    for entry in provenance:
        number = getattr(entry, "page_no", None)
        if isinstance(number, int):
            return number
    return None


def _docling_text_of(item: object, document: object) -> str:
    """Flatten one document item to text, keeping tables as markdown.

    Markdown is not decoration here. A table rendered as pipe-delimited rows
    survives the figure-debris filter, whereas the same table as loose cells
    is exactly the pattern that filter exists to delete -- so the format
    choice is what stops layer 2 removing the content layer 2 just recovered.
    """
    render = getattr(item, "export_to_markdown", None)
    if render is not None and hasattr(getattr(item, "data", None), "grid"):
        # Ask the signature whether it wants the document, rather than
        # calling and catching. Catching TypeError here looks equivalent and
        # is not: it also swallows a TypeError raised *inside* the
        # serializer, quietly downgrading to a deprecated path that produces
        # a worse table while reporting nothing.
        rendered = render(doc=document) if _accepts_doc(render) else render()
        if rendered:
            return str(rendered).strip()

    text = getattr(item, "text", None)
    return str(text).strip() if text else ""


@lru_cache(maxsize=8)
def _accepts_doc_function(function: object) -> bool:
    import inspect

    try:
        return "doc" in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def _accepts_doc(method: object) -> bool:
    """Whether a bound method takes a `doc` argument, cached per function."""
    return _accepts_doc_function(getattr(method, "__func__", method))


def _docling_page_count(document: object) -> int:
    pages = getattr(document, "pages", None)
    try:
        return len(pages) if pages is not None else 0
    except TypeError:
        return 0


@lru_cache(maxsize=2)
def _docling_converter(formulas: bool):
    """Build and keep one converter per configuration.

    Cached because instantiating it loads the layout and table models. Paying
    that once per process instead of once per file is the difference between
    a slow ingest and an unusable one across a folder of thirty lectures.
    """
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise OverfitError(
            "PDF_BACKEND is set to a docling option, but docling is not "
            "installed.\n"
            "    uv sync --extra docling\n"
            "Or switch back -- the choice is configuration, not code:\n"
            "    PDF_BACKEND=pypdf"
        ) from exc

    options = PdfPipelineOptions()
    options.do_formula_enrichment = formulas
    # Off deliberately, and it has to be: the optional dependency installs a
    # PDF pipeline without any OCR engine, so leaving this on its default
    # would fail at convert time rather than at install time. It is also the
    # right behaviour -- lecture PDFs are digital, OCR would cost minutes per
    # deck to re-read text that is already there, and a genuine scan should
    # still surface as EmptyExtractionError rather than be silently guessed
    # at. To enable it, add an engine (`docling-slim[feat-ocr-rapidocr]`)
    # before flipping this.
    options.do_ocr = False
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


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


# Dispatch happens on two axes, and they are not the same question.
#
# Extension decides *what kind of file* this is, and there is exactly one
# sensible reader for a text file. PDFs are the exception: the format is
# print-oriented enough that different libraries disagree substantially
# about what the text even is, so the reader is a choice the user makes and
# the index records.
_TEXT_BACKENDS: dict[str, Callable[[Path], list[Page]]] = {
    ".md": _parse_text,
    ".markdown": _parse_text,
    ".txt": _parse_text,
}

_PDF_BACKENDS: dict[str, Callable[[Path], list[Page]]] = {
    "pypdf": _parse_pdf,
    "docling": lambda path: _parse_docling(path, formulas=False),
    "docling+formula": lambda path: _parse_docling(path, formulas=True),
}


def _read(path: Path, backend: str) -> list[Page]:
    """Extract raw pages, before any cleaning."""
    suffix = path.suffix.lower()
    if suffix in _TEXT_BACKENDS:
        return _TEXT_BACKENDS[suffix](path)
    if suffix == ".pdf":
        reader = _PDF_BACKENDS.get(backend)
        if reader is None:
            raise ValueError(
                f"unknown pdf backend {backend!r}; "
                f"expected one of {', '.join(sorted(_PDF_BACKENDS))}"
            )
        return reader(path)
    raise UnsupportedFormatError(path)


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

# A markdown table row, i.e. something a layout-aware backend reconstructed
# rather than something extraction flattened. The distinction is the whole
# basis of the debris rule: it exists to delete text whose structure was
# *lost*, and a table row is text whose structure was *recovered*. Without
# this exemption a narrow table is indistinguishable from debris -- rows like
# "|   Age | HighRisk   |" are barely twenty characters, carry no terminal
# punctuation and are not bullets, so six of them in a row look exactly like
# a flattened chart and the whole table is deleted. That failure is worse
# than the one the rule was written for: the content was successfully
# recovered upstream and then destroyed here.
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")


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
    keep = _debris_mask(lines)
    return "\n".join(line for line, wanted in zip(lines, keep) if wanted)


def _debris_mask(lines: list[str]) -> list[bool]:
    """True for every line worth keeping.

    Split out from `drop_figure_debris` so that the diagnostic can report
    exactly which lines were discarded without re-deriving the rule. The
    decision lives here once; both callers only apply it.
    """
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

    return keep


def _is_fragment(line: str) -> bool:
    """A line that could plausibly be a stray label rather than a sentence."""
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) > _DEBRIS_MAX_CHARS:
        return False
    if _TABLE_ROW.match(line):
        return False  # reconstructed structure, not lost structure
    if _BULLET_LINE.match(line):
        return False  # a real bullet, however terse
    if stripped[-1] in ".!?:":
        return False  # finished sentences are content
    return True


def find_running_lines(pages: list[Page]) -> dict[str, int]:
    """Identify headers and footers that repeat across the document.

    Detected statistically rather than by rule: any first or last line that
    shows up on most pages is furniture. This catches unit codes, lecturer
    names and copyright notices without needing to know anything about the
    specific document -- which matters, because course material comes from
    dozens of different templates.

    Returns each condemned line with the number of pages it was *detected* on,
    so a reader can see the margin by which it crossed the threshold. A line
    found on every page is obviously furniture; one that scrapes past 60% is
    the kind of call worth checking by eye, and the count is the only way to
    tell those two apart after the fact.

    Note the asymmetry with how the verdict is applied: detection looks only
    at the outer edges of a page, because a repeated line in the middle is far
    more likely to be a genuine recurring definition -- but removal then takes
    the line out wherever it appears. That is deliberate (a header does not
    stop being a header when extraction misplaces it) and it is also the most
    plausible route to deleting real content, which is why the diagnostic
    reports every occurrence rather than a count.
    """
    if len(pages) < _MIN_PAGES_FOR_HEADER_DETECTION:
        return {}

    counts: Counter[str] = Counter()
    for page in pages:
        lines = [line.strip() for line in page.text.splitlines() if line.strip()]
        if not lines:
            continue
        counts.update({lines[0], lines[-1]})

    threshold = len(pages) * _HEADER_REPEAT_RATIO
    return {line: count for line, count in counts.items() if count >= threshold}


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
