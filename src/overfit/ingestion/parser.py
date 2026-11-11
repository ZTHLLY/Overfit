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

import hashlib
import re
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from overfit.errors import (
    EmptyExtractionError,
    ExtractionError,
    OverfitError,
    UnsupportedFormatError,
)
from overfit.models import Page, ParsedDocument

__all__ = [
    "CleaningReport",
    "Removal",
    "clean_page",
    "cleaning_report",
    "find_running_lines",
    "parse",
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
    # True when the backend named the furniture, False when a 60% frequency
    # threshold guessed it. Worth surfacing: a narrow margin is a reason to
    # look twice at a guess and means nothing at all about a label.
    furniture_labelled: bool = False

    @property
    def removed_ratio(self) -> float:
        if not self.chars_before:
            return 0.0
        return 1.0 - self.chars_after / self.chars_before

    def by_stage(self, stage: str) -> list[Removal]:
        return [item for item in self.removals if item.stage == stage]

    def furniture_lines(self) -> tuple[int, list[tuple[str, int]]]:
        """Bare page numbers as a count, everything else listed, commonest first.

        The two ways furniture is found produce very different shapes, and one
        display cannot serve both. A frequency threshold finds *one* line
        repeated across most pages. A layout model labels *every* block it
        considers furniture, and on a slide deck that is one page number per
        page -- seventy entries of "1 page (1%)", which buries the handful of
        lines actually worth reading.

        Numbers are the part nobody needs to see individually; the rest is the
        part where a misclassified heading would be hiding.
        """
        numbers = 0
        named: list[tuple[str, int]] = []
        for line, count in self.furniture.items():
            if _is_bare_page_number(line):
                numbers += count
            else:
                named.append((line, count))
        named.sort(key=lambda pair: (-pair[1], pair[0]))
        return numbers, named

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

    labelled = Counter(line for page in raw for line in set(page.furniture))

    return CleaningReport(
        source=source or path.name,
        pages=len(raw),
        chars_before=sum(len(page.text) for page in raw),
        chars_after=sum(len(page.text) for page in cleaned),
        furniture=dict(labelled) if labelled else find_running_lines(raw),
        furniture_labelled=bool(labelled),
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
    if path.suffix.lower() not in _TEXT_BACKENDS and path.suffix.lower() != ".pdf":
        raise UnsupportedFormatError(path)
    digest = _source_digest(path)
    raw = _read(path, backend)
    if _source_digest(path) != digest:
        raise ExtractionError(
            path, "source changed during extraction; retry with a stable file"
        )
    raw = [
        replace(p, provenance={**p.provenance, "document_sha256": digest})
        if p.provenance
        else p
        for p in raw
    ]
    pages = _clean(raw)
    metadata = deepcopy(raw[0].provenance) if raw else None
    if metadata is not None:
        metadata["blocks"] = []
        for page in raw:
            for block in (page.provenance or {}).get("blocks", []):
                block = deepcopy(block)
                block["text_span"] = None
                block["mapping"] = "coarse"
                metadata["blocks"].append(block)
    document = ParsedDocument(
        source=source or path.name, pages=pages, provenance=metadata
    )

    # Fail loudly rather than letting a blank document into the index. A
    # capability may be missing; it must not be faked.
    if document.is_empty:
        raise EmptyExtractionError(path)

    return document


def _source_digest(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ExtractionError(path, str(exc)) from exc


def _clean(pages: list[Page], record: list[Removal] | None = None) -> list[Page]:
    """Run the three cleaning stages, optionally recording what they removed.

    `parse` and `cleaning_report` both go through here, and that is the point.
    Cleaning is lossy and irreversible, so an audit of it is only worth
    trusting if it describes the same code that ran -- a second
    implementation written for the report would start accurate and drift.
    """
    labelled = {line for page in pages for line in page.furniture}
    protected = frozenset(line for page in pages for line in page.structured)

    # Below a handful of pages the frequency test is meaningless -- a 3-page
    # handout may legitimately repeat a phrase at the top of every page -- so
    # the whole stage is skipped, page numbers included. A backend that
    # labelled the furniture itself is exempt from that caution: it did not
    # count anything, so there is nothing for a small sample to distort.
    long_enough = len(pages) >= _MIN_PAGES_FOR_HEADER_DETECTION
    if labelled:
        furniture = labelled
    elif long_enough:
        furniture = set(find_running_lines(pages))
    else:
        furniture = set()

    stripped: list[Page] = []
    for page in pages:
        # Bare page numbers are dropped alongside furniture rather than by
        # their own rule, and share its guard: on a three-page handout a lone
        # "2" is as likely to be an answer as a page number.
        if not (labelled or long_enough):
            stripped.append(page)
            continue
        kept: list[str] = []
        for line in page.text.splitlines():
            if line.strip() and line.strip() in furniture:
                _note(record, page.number, "furniture", line)
            elif _is_bare_page_number(line):
                _note(record, page.number, "page-number", line)
            else:
                kept.append(line)
        stripped.append(_replacing(page, "\n".join(kept)))

    debrided: list[Page] = []
    for page in stripped:
        lines = page.text.splitlines()
        keep = _debris_mask(lines, protected)
        for line, wanted in zip(lines, keep):
            if not wanted:
                _note(record, page.number, "figure-debris", line)
        debrided.append(
            _replacing(page, "\n".join(l for l, w in zip(lines, keep) if w))
        )

    return [_replacing(p, clean_page(p.text)) for p in debrided]


def _replacing(page: Page, text: str) -> Page:
    """A copy with new text, keeping the number and the backend's verdicts."""
    return Page(
        number=page.number,
        text=text,
        furniture=page.furniture,
        structured=page.structured,
        provenance=_remap(page, text),
    )


def _remap(page: Page, text: str) -> dict | None:
    """Carry only demonstrable surviving ranges; original blocks stay auditable."""
    metadata = deepcopy(page.provenance)
    if metadata is None or page.text == text:
        return metadata
    opcodes = SequenceMatcher(None, page.text, text, autojunk=False).get_opcodes()
    for block in metadata["blocks"]:
        span = block["text_span"]
        if span is None:
            continue
        a, b = span
        old = page.text[a:b]
        # Repeated fragments cannot be attributed by a textual diff alone.
        ambiguous = bool(old) and page.text.count(old) > 1
        ranges = []
        for kind, i, j, x, y in opcodes:
            if kind == "equal" and max(a, i) < min(b, j):
                ranges.append((x + max(a, i) - i, x + min(b, j) - i))
            elif kind == "replace" and max(a, i) < min(b, j) and x < y:
                # A replacement crossing block boundaries has no safe owner.
                if a <= i and j <= b:
                    ranges.append((x, y))
            elif kind == "insert" and a < i < b and x < y:
                ranges.append((x, y))
        if not ranges or ambiguous:
            block["text_span"] = None
            block["mapping"] = "coarse"
            continue
        start, end = min(r[0] for r in ranges), max(r[1] for r in ranges)
        block["text_span"] = [start, end]
        bs = block["block_span"]
        if not (
            block["mapping"] == "exact"
            and bs is not None
            and text[start:end] == block["text"][bs[0] : bs[1]]
        ):
            block["mapping"] = "coarse"
    return metadata


def _metadata(extractor: str, count: int | None, blocks: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "document_sha256": None,
        "page_count": count,
        "extractor": extractor,
        "blocks": blocks,
    }


def _extractor(package: str) -> str:
    try:
        return f"{package}/{version(package)}"
    except PackageNotFoundError:
        return package


def _plain_page(number: int, text: str, count: int | None, extractor: str) -> Page:
    block = {
        "block_id": f"page:{number}",
        "label": "page_text",
        "parent_id": None,
        "context_id": None,
        "text": text,
        "locations": [{"page": number, "charspan": [0, len(text)], "bbox": None}],
        "text_span": [0, len(text)],
        "block_span": [0, len(text)],
        "mapping": "exact",
    }
    return Page(number, text, provenance=_metadata(extractor, count, [block]))


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
            except Exception as exc:
                raise ExtractionError(path, f"encrypted PDF ({exc})") from exc
        return [
            _plain_page(
                index, page.extract_text() or "", len(reader.pages), _extractor("pypdf")
            )
            for index, page in enumerate(reader.pages, start=1)
        ]
    except ExtractionError:
        raise
    except Exception as exc:
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

    Every known block location is retained even when its rendered text is
    placed in the first page's bucket. A trusted physical count preserves
    empty pages; partial extractor page maps never invent a document total.
    Picture children are isolated as figure text, not promoted to prose.
    """
    converter = _docling_converter(formulas)
    try:
        result = converter.convert(str(path))
        document = result.document
        count = getattr(getattr(result, "input", None), "page_count", None)
        count = count if type(count) is int and count > 0 else None
        items = list(_docling_items(document))
        by_id = {str(getattr(item, "self_ref", "")): item for item, _ in items}
        caption_contexts: dict[str, list[str]] = {}
        for ref, item in by_id.items():
            if ref and _docling_label_of(item) == "picture":
                for caption in getattr(item, "captions", []) or []:
                    if caption_ref := _docling_ref(caption):
                        caption_contexts.setdefault(caption_ref, []).append(ref)
        buckets: dict[int, list[str]] = {}
        blocks: dict[int, list[dict]] = {}
        furniture: dict[int, list[str]] = {}
        structured: dict[int, list[str]] = {}
        offsets: dict[int, int] = {}
        last_context: dict[int, str | None] = {}
        for index, (item, _level) in enumerate(items):
            locations = _docling_locations(item)
            if not locations:
                continue
            number = locations[0]["page"]
            label = _docling_label_of(item) or "unknown"
            raw_text = _docling_text_of(item, document)
            parent = _docling_ref(getattr(item, "parent", None))
            contexts = list(
                dict.fromkeys(
                    caption_contexts.get(str(getattr(item, "self_ref", "")), [])
                )
            )
            if (ancestor := _picture_context(parent, by_id)) and ancestor not in contexts:
                contexts.insert(0, ancestor)
            context = contexts[0] if contexts else None
            parts = buckets.setdefault(number, [])
            offset = offsets.get(number, 0)
            # Isolate extracted figure labels; do not present them as prose.
            if last_context.get(number) and last_context[number] != context:
                marker = "[End figure text]"
                offset += (2 if parts else 0) + len(marker)
                parts.append(marker)
                structured.setdefault(number, []).append(marker)
            if context and last_context.get(number) != context:
                marker = "[Figure text; spatial relationships not reconstructed]"
                offset += (2 if parts else 0) + len(marker)
                parts.append(marker)
                structured.setdefault(number, []).append(marker)
            last_context[number] = context
            start = offset + (2 if parts and raw_text else 0)
            if raw_text:
                parts.append(raw_text)
                offset = start + len(raw_text)
            offsets[number] = offset
            is_table = hasattr(getattr(item, "data", None), "grid")
            original = str(getattr(item, "text", raw_text) or raw_text)
            # Markdown and glyph coordinates are different domains.
            exact = not is_table and original == raw_text
            block = {
                "block_id": str(getattr(item, "self_ref", None) or f"item:{index}"),
                "label": label,
                "parent_id": parent,
                "context_id": context,
                "text": original,
                "locations": locations,
                "text_span": [start, start + len(raw_text)] if raw_text else None,
                "block_span": [0, len(original)],
                "mapping": "exact" if exact and raw_text else "coarse",
            }
            # A caption may be explicitly shared by multiple pictures. Keep
            # each known relation over the same carrier, without duplicating
            # its actual text or guessing relations from proximity.
            for related in contexts or [None]:
                blocks.setdefault(number, []).append({**block, "context_id": related})
            if _docling_layer_of(item) != "body":
                furniture.setdefault(number, []).extend(_lines(raw_text))
            elif label not in _PROSE_LABELS or context:
                structured.setdefault(number, []).extend(_lines(raw_text))
        for number, context in last_context.items():
            if context:
                buckets[number].append("[End figure text]")
                structured.setdefault(number, []).append("[End figure text]")
        known = {
            loc["page"]
            for group in blocks.values()
            for block in group
            for loc in block["locations"]
        }
        known.update(
            k
            for k in (getattr(document, "pages", {}) or {})
            if type(k) is int and k > 0
        )
        if count is not None and any(number > count for number in known):
            raise ValueError("Extractor location exceeds physical page count")
        numbers = range(1, count + 1) if count is not None else sorted(known)
        return [
            Page(
                number,
                "\n\n".join(buckets.get(number, [])),
                tuple(furniture.get(number, [])),
                tuple(structured.get(number, [])),
                _metadata(_extractor("docling-slim"), count, blocks.get(number, [])),
            )
            for number in numbers
        ]
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(path, f"{type(exc).__name__}: {exc}") from exc


def _docling_ref(value) -> str | None:
    ref = getattr(value, "cref", None)
    return str(ref) if ref else None


def _picture_context(parent: str | None, items: dict) -> str | None:
    seen = set()
    while parent and parent not in seen:
        seen.add(parent)
        item = items.get(parent)
        if item is None:
            return None
        if _docling_label_of(item) == "picture":
            return parent
        parent = _docling_ref(getattr(item, "parent", None))
    return None


def _docling_locations(item) -> list[dict]:
    locations = []
    for entry in getattr(item, "prov", []) or []:
        page = getattr(entry, "page_no", None)
        if type(page) is not int or page < 1:
            raise ValueError("Invalid extractor page")
        bbox = getattr(entry, "bbox", None)
        box = None
        if bbox is not None:
            origin = getattr(bbox, "coord_origin", None)
            box = {key: getattr(bbox, key) for key in ("l", "t", "r", "b")}
            box["coord_origin"] = str(getattr(origin, "value", origin) or "unknown")
        span = getattr(entry, "charspan", None)
        locations.append(
            {
                "page": page,
                "charspan": list(span) if span is not None else None,
                "bbox": box,
            }
        )
    return locations


# From docling's DocItemLabel. Only `text` is prose; everything else is a
# block whose shape was recognised, and recognised shapes are precisely what
# the debris rule must leave alone.
_PROSE_LABELS = frozenset({"text"})


def _docling_layer_of(item: object) -> str:
    """Which content layer a block was filed under: body, furniture, notes..."""
    layer = getattr(item, "content_layer", None)
    return str(getattr(layer, "value", layer) or "body")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _docling_items(document: object):
    """Walk the document, including the layer docling would hide by default.

    `iterate_items` traverses only `ContentLayer.BODY`. Page headers and
    footers live in `ContentLayer.FURNITURE` and are therefore dropped before
    this module ever sees them -- which sounds convenient and is the exact
    failure this pipeline keeps trying to avoid. Two reasons it is not
    acceptable:

    * The deletion happens upstream of cleaning, so `cleaning_report` cannot
      show it. A layout model that mistakes a real section heading for a page
      header removes it with no trace anywhere.
    * It is unreliable in a way that hides its own unreliability. On this
      corpus the same footer was assigned to FURNITURE on some pages and to
      BODY on others, so the frequency heuristic silently found it on 65% of
      pages instead of 100% and reported a borderline call -- covering for a
      misclassification nobody could see.

    Asking for both layers puts every decision back in front of the cleaning
    stage, which removes furniture too, but records it while doing so.
    """
    kwargs = {}
    method = document.iterate_items
    function = getattr(method, "__func__", method)
    if _accepts_parameter(function, "traverse_pictures"):
        kwargs["traverse_pictures"] = True
    if _accepts_parameter(function, "with_groups"):
        kwargs["with_groups"] = True
    try:
        from docling_core.types.doc import ContentLayer
    except ImportError:  # optional SDK; synthetic adapters need no dependency
        pass
    else:
        if _accepts_layers(method):
            kwargs["included_content_layers"] = {
                ContentLayer.BODY,
                ContentLayer.FURNITURE,
            }
    return method(**kwargs)


def _accepts_layers(method: object) -> bool:
    return _accepts_parameter(
        getattr(method, "__func__", method), "included_content_layers"
    )


@lru_cache(maxsize=8)
def _accepts_parameter(function: object, name: str) -> bool:
    import inspect

    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def _docling_label_of(item: object) -> str:
    """The block's classification, as a plain string.

    DocItemLabel is a str enum, so `.value` is taken rather than str() --
    the latter renders as "DocItemLabel.PAGE_FOOTER" on some Python versions
    and would silently match nothing.
    """
    label = getattr(item, "label", None)
    return str(getattr(label, "value", label) or "")


def _docling_page_of(item: object) -> int | None:
    """The 1-based page an item sits on, or None if it claims no position."""
    provenance = getattr(item, "prov", None) or []
    for entry in provenance:
        number = getattr(entry, "page_no", None)
        if type(number) is int and number > 0:
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


def _accepts_doc(method: object) -> bool:
    """Whether a bound method takes a `doc` argument, cached per function."""
    return _accepts_parameter(getattr(method, "__func__", method), "doc")


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

    Text files have no physical pages. They are represented as one logical
    page, so the current citation renderer prints ``p1``; that is a synthetic
    location for the whole file, not a claim that the source is paginated.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExtractionError(path, str(exc)) from exc
    return [_plain_page(1, content, None, "text/utf-8")]


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


def _debris_mask(
    lines: list[str], protected: frozenset[str] = frozenset()
) -> list[bool]:
    """True for every line worth keeping.

    Split out from `drop_figure_debris` so that the diagnostic can report
    exactly which lines were discarded without re-deriving the rule. The
    decision lives here once; both callers only apply it.

    `protected` holds lines the backend already classified as a table, an
    equation or a heading. This rule exists to delete text whose structure
    was *lost*; a line that arrived with its structure named is outside its
    jurisdiction, whatever it looks like.
    """
    flags = [_is_fragment(line) and line.strip() not in protected for line in lines]

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
    # Finished sentences are content, not stray labels.
    return stripped[-1] not in ".!?:"


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
