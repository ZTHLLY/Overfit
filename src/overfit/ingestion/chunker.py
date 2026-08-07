"""Layer 3 -- Chunker.

Splits a parsed document into retrievable units.

The central design decision here is what a page boundary *means*. It is
tempting to classify each file as "slides" or "document" and chunk
accordingly, but that classification is both unreliable (landscape reports,
A4 handouts, mixed decks) and unnecessary. The question that actually
matters is narrower and locally answerable:

    does the text on page N continue onto page N+1?

That is directly observable -- a page ending without terminal punctuation
followed by one starting in lower case is a sentence carrying over -- so it
is decided per boundary, on evidence, instead of per file, on a proxy. A deck
of slides keeps its boundaries because each slide ends cleanly; a report gets
merged because its sentences run on; a hybrid file gets both, which no global
classifier can manage.

When the evidence is ambiguous we do not merge. The two failure modes are not
symmetric: merging unrelated pages blends topics into one vector and fails
*silently*, while an over-eager split severs one sentence, is partly repaired
by overlap, and is obvious the moment a human reads a chunk.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from overfit.models import Chunk, Page, ParsedDocument

__all__ = ["chunk_document", "CHARS_PER_TOKEN"]


# Chunk sizes are configured in tokens because that is what limits a model,
# but measured in characters because counting tokens exactly would mean an
# API call per chunk. Four characters per token is the usual English
# approximation, and chunking is a fuzzy craft: roughly even sizes are all
# that is required.
CHARS_PER_TOKEN = 4

# Terminal punctuation. A page ending in any of these has finished its
# thought, so the following page starts something new.
_SENTENCE_END = frozenset(".!?:;。！？…" + "\"')]}”’")

# Bullet glyphs. A page ending mid-bullet-list is still structurally complete
# -- list items are self-contained -- so these count as closed.
_BULLET_START = re.compile(r"^\s*([•●▪–—\-*·]|\d+[.)]|[a-z][.)])\s")

# Break candidates, strongest first. The chunker prefers to cut where a human
# would: between paragraphs, then between lines, then between sentences, and
# only between words as a last resort.
_RANK_PARAGRAPH, _RANK_LINE, _RANK_SENTENCE, _RANK_WORD = 4, 3, 2, 1

# How far back from the ideal cut point we are willing to look for a good
# boundary. Beyond this the chunk gets too short to be worth the tidiness.
_BACKTRACK_RATIO = 0.35

# Chunks below this fraction of the target are folded into the previous one
# rather than stored alone -- a 20-character fragment retrieves badly and
# tells the model nothing.
_MIN_CHUNK_RATIO = 0.15

# A chunk shorter than this is dropped outright. Real course material throws
# up plenty of these: divider slides reading "Questions?", section titles,
# a stray caption. They carry no answerable content, yet each one occupies a
# row in the index and can win a top-k slot away from something useful.
_MIN_CHUNK_CHARS = 40


@dataclass(frozen=True, slots=True)
class _Segment:
    """A run of pages whose text flows continuously.

    `text` is the concatenation; `starts[i]` is the offset at which
    `pages[i]` begins, so any offset can be mapped back to the page it came
    from. This is what lets chunk boundaries ignore pages while citations
    still know exactly where the text lives.
    """

    text: str
    starts: list[int]
    pages: list[int]

    def page_at(self, offset: int) -> int:
        index = bisect.bisect_right(self.starts, offset) - 1
        return self.pages[max(index, 0)]


def chunk_document(
    document: ParsedDocument,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Split a document into chunks, each carrying its own provenance.

    Args:
        chunk_size: target size in tokens.
        chunk_overlap: tokens repeated between neighbours, so a sentence cut
            at a boundary still appears whole in one of the two chunks.
    """
    size = chunk_size * CHARS_PER_TOKEN
    overlap = min(chunk_overlap * CHARS_PER_TOKEN, size - 1)

    chunks: list[Chunk] = []
    index = 0

    for segment in _segment(document.pages):
        for start, end in _split_positions(segment.text, size, overlap):
            text = segment.text[start:end].strip()
            if len(text) < _MIN_CHUNK_CHARS:
                continue
            page_start = segment.page_at(start)
            page_end = segment.page_at(end - 1)
            chunks.append(
                Chunk(
                    id=Chunk.make_id(document.source, page_start, index),
                    text=text,
                    source=document.source,
                    page=page_start,
                    page_end=page_end if page_end != page_start else None,
                )
            )
            index += 1

    return chunks


# ---------------------------------------------------------------------------
# Step 1: decide where pages really break
# ---------------------------------------------------------------------------


def _segment(pages: list[Page]) -> list[_Segment]:
    """Group pages into runs of continuous text."""
    segments: list[_Segment] = []
    buffer: list[Page] = []

    for page in pages:
        if not page.text.strip():
            continue  # blank pages carry nothing and would only skew offsets
        if buffer and not _continues(buffer[-1].text, page.text):
            segments.append(_build_segment(buffer))
            buffer = []
        buffer.append(page)

    if buffer:
        segments.append(_build_segment(buffer))
    return segments


def _build_segment(pages: list[Page]) -> _Segment:
    parts: list[str] = []
    starts: list[int] = []
    numbers: list[int] = []
    offset = 0

    for position, page in enumerate(pages):
        # A newline between pages, so a page break never fuses two words.
        joiner = "\n" if position else ""
        offset += len(joiner)
        starts.append(offset)
        numbers.append(page.number)
        parts.append(joiner + page.text)
        offset += len(page.text)

    return _Segment(text="".join(parts), starts=starts, pages=numbers)


def _continues(previous: str, following: str) -> bool:
    """True when `following` looks like the continuation of `previous`.

    Both halves must agree: the earlier page has to end unfinished *and* the
    later one has to start unfinished. Requiring both keeps the decision
    conservative, which is the direction we want to err in.
    """
    before = previous.rstrip()
    after = following.lstrip()
    if not before or not after:
        return False

    # A completed sentence, a heading, or a bullet ends the thought.
    if before[-1] in _SENTENCE_END:
        return False

    first_line = after.splitlines()[0] if after.splitlines() else after
    if _BULLET_START.match(first_line):
        return False

    # Capitals usually start something new: a heading, a name, a new sentence.
    return after[0].islower()


# ---------------------------------------------------------------------------
# Step 2: split a segment at humane boundaries
# ---------------------------------------------------------------------------

_PARAGRAPH = re.compile(r"\n\s*\n")
_LINE = re.compile(r"\n")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“])")
_WORD = re.compile(r"\s+")


def _break_candidates(text: str) -> tuple[list[int], list[int]]:
    """Offsets where a cut would be acceptable, with a preference rank."""
    ranked: dict[int, int] = {}
    for pattern, rank in (
        (_WORD, _RANK_WORD),
        (_SENTENCE, _RANK_SENTENCE),
        (_LINE, _RANK_LINE),
        (_PARAGRAPH, _RANK_PARAGRAPH),
    ):
        for match in pattern.finditer(text):
            # Later patterns overwrite earlier ones, so stronger ranks win.
            ranked[match.end()] = rank

    offsets = sorted(ranked)
    return offsets, [ranked[o] for o in offsets]


def _split_positions(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Walk the text, producing (start, end) spans.

    Spans stay contiguous and overlapping -- the next one begins slightly
    before the previous ends -- so every chunk still maps onto a real stretch
    of the segment and its pages can be looked up by offset.
    """
    if len(text) <= size:
        return [(0, len(text))] if text.strip() else []

    offsets, ranks = _break_candidates(text)
    spans: list[tuple[int, int]] = []
    start = 0
    minimum = int(size * _MIN_CHUNK_RATIO)

    while start < len(text):
        target = start + size
        if target >= len(text):
            # Fold a stub tail into the previous chunk instead of storing it
            # on its own.
            if spans and len(text) - start < minimum:
                spans[-1] = (spans[-1][0], len(text))
            else:
                spans.append((start, len(text)))
            break

        end = _best_break(offsets, ranks, start, target, size)
        spans.append((start, end))
        start = _overlap_start(offsets, start, end, overlap)

    return spans


def _overlap_start(offsets: list[int], start: int, end: int, overlap: int) -> int:
    """Where the next chunk begins, so that it repeats `overlap` characters.

    The naive `end - overlap` lands wherever it lands -- frequently inside a
    word, producing chunks that open with "ython is free...". Snapping
    forward to the next real boundary costs a few characters of overlap and
    buys a chunk that reads correctly, both for the embedding model and for
    the human debugging it.
    """
    target = max(end - overlap, start + 1)
    index = bisect.bisect_left(offsets, target)
    if index < len(offsets) and target <= offsets[index] < end:
        return offsets[index]
    return target


def _best_break(
    offsets: list[int],
    ranks: list[int],
    start: int,
    target: int,
    size: int,
) -> int:
    """Pick the nicest cut at or before `target`.

    Searches backwards within a window and takes the strongest boundary
    available, preferring a paragraph break slightly early over a word break
    exactly on target. Falls back to a hard cut when the text has no
    whitespace at all (a long table row, say).
    """
    floor = max(start + 1, target - int(size * _BACKTRACK_RATIO))

    low = bisect.bisect_left(offsets, floor)
    high = bisect.bisect_right(offsets, target)
    if low >= high:
        return target  # nothing usable nearby: cut mid-word rather than loop

    best_index = max(range(low, high), key=lambda i: (ranks[i], offsets[i]))
    return offsets[best_index]
