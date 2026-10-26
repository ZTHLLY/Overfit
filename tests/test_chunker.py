"""Chunk slicing carries provenance without introducing audit-only text."""

from overfit.ingestion.chunker import chunk_document
from overfit.ingestion.parser import _plain_page, _replacing
from overfit.models import Page, ParsedDocument
from overfit.provenance import provenance_pages


def test_exact_chunks_retain_original_text_and_clip_two_coordinate_spans():
    original = "  " + "A useful synthetic sentence has distinct content. " * 10 + "  "
    page = _plain_page(3, original, 8, "test")
    chunks = chunk_document(ParsedDocument("doc.pdf", [page]), 30, 4)
    assert len(chunks) > 2
    for chunk in chunks:
        block = chunk.provenance["blocks"][0]
        assert block["text"] == original
        assert block["mapping"] == "exact"
        a, b = block["block_span"]
        assert original[a:b] == chunk.text
        assert block["text_span"] == [0, len(chunk.text)]
        assert provenance_pages(chunk.provenance) == [3]


def test_multiple_provenance_pages_survive_first_page_projection():
    text = "First part with sufficient content; second part with sufficient content."
    page = _plain_page(5, text, 9, "test")
    page.provenance["blocks"][0]["locations"] = [
        {"page": 5, "charspan": [0, 35], "bbox": None},
        {"page": 6, "charspan": [35, len(text)], "bbox": None},
    ]
    chunk = chunk_document(ParsedDocument("doc.pdf", [page]), 250, 25)[0]
    assert chunk.page == 5 and chunk.page_end == 6
    assert provenance_pages(chunk.provenance) == [5, 6]


def test_exact_slice_can_narrow_pages_but_keeps_original_location_coordinates():
    text = "A" * 48 + "B" * 48
    page = _plain_page(5, text, 9, "test")
    page.provenance["blocks"][0]["locations"] = [
        {"page": 5, "charspan": [0, 48], "bbox": None},
        {"page": 6, "charspan": [48, 96], "bbox": None},
    ]
    chunks = chunk_document(ParsedDocument("doc.pdf", [page]), 12, 0)
    assert [c.page for c in chunks] == [5, 6]
    assert all(c.page_end is None for c in chunks)
    assert chunks[1].provenance["blocks"][0]["locations"][0]["charspan"] == [48, 96]
    assert len(page.provenance["blocks"][0]["locations"]) == 2


def test_coarse_multi_page_slice_never_discards_possible_pages():
    text = "X" * 96
    page = _plain_page(5, text, 9, "test")
    page.provenance["blocks"][0].update(
        mapping="coarse",
        locations=[
            {"page": 5, "charspan": [0, 48], "bbox": None},
            {"page": 8, "charspan": [48, 96], "bbox": None},
        ],
    )
    chunks = chunk_document(ParsedDocument("doc.pdf", [page]), 12, 0)
    assert all(provenance_pages(c.provenance) == [5, 8] for c in chunks)
    assert all(c.page_end == 8 for c in chunks)


def test_cleaning_removed_block_never_enters_chunk_evidence():
    text = "Audit only content."
    removed = _replacing(_plain_page(4, text, 9, "test"), "")
    page = _plain_page(
        5, "Actual evidence with enough characters to form a complete chunk.", 9, "test"
    )
    page.provenance["blocks"].extend(removed.provenance["blocks"])
    chunk = chunk_document(ParsedDocument("doc.pdf", [page]), 250, 25)[0]
    assert provenance_pages(chunk.provenance) == [5]
    assert text not in chunk.text
    assert all(b["text"] != text for b in chunk.provenance["blocks"])


def test_continuation_across_blank_page_keeps_real_page_set():
    first = _plain_page(
        2, "A sentence with enough words begins here and continues", 8, "test"
    )
    last = _plain_page(
        4, "onto a nonadjacent physical page without losing its location.", 8, "test"
    )
    chunks = chunk_document(
        ParsedDocument("doc.pdf", [first, Page(3, ""), last]), 250, 25
    )
    assert len(chunks) == 1
    assert provenance_pages(chunks[0].provenance) == [2, 4]
    assert chunks[0].page_end == 4


def test_mixed_metadata_preserves_known_blocks_without_claiming_unknown_text():
    first = _plain_page(
        2, "A sentence with enough words begins here and continues", 8, "test"
    )
    last = Page(3, "onto a legacy page without trustworthy original metadata.")
    chunk = chunk_document(ParsedDocument("doc.pdf", [first, last]), 250, 25)[0]
    assert provenance_pages(chunk.provenance) == [2]
    assert chunk.provenance["page_count"] is None
    assert chunk.provenance["document_sha256"] is None
    assert chunk.provenance["blocks"][0]["text_span"] == [0, len(first.text)]
    assert chunk.provenance["blocks"][0]["text"] == first.text
    assert chunk.page == 2 and chunk.page_end == 3
    legacy = chunk_document(ParsedDocument("doc.pdf", [last]), 250, 25)[0]
    assert legacy.provenance is None


def test_unknown_extractor_coordinate_domain_cannot_narrow_pages():
    text = "A" * 96
    for spans in ([None, [0, 48]], [[0, 48], [800, 900]], [[0, 10], [60, 96]]):
        page = _plain_page(5, text, 9, "test")
        page.provenance["blocks"][0]["locations"] = [
            {"page": 5, "charspan": spans[0], "bbox": None},
            {"page": 8, "charspan": spans[1], "bbox": None},
        ]
        chunks = chunk_document(ParsedDocument("doc.pdf", [page]), 12, 0)
        assert provenance_pages(chunks[0].provenance) == [5, 8]
        assert chunks[0].page_end == 8


def test_conflicting_page_metadata_fails_instead_of_picking_first_identity():
    import pytest

    first = _plain_page(
        2, "A sentence with enough words begins here and continues", 8, "test"
    )
    last = _plain_page(
        3, "onto another physical page with conflicting document identity.", 8, "test"
    )
    first.provenance["document_sha256"] = "a" * 64
    last.provenance["document_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="Conflicting"):
        chunk_document(ParsedDocument("doc.pdf", [first, last]), 250, 25)


def test_middle_of_long_figure_keeps_structure_in_actual_generation_prompt():
    from copy import deepcopy

    from overfit.query.generator import _templates

    marker = "[Figure text; spatial relationships not reconstructed]\n"
    figure = "".join(f"axis label {index}: category; " for index in range(80))
    text = marker + figure + "\n[End figure text]"
    page = _plain_page(5, text, 9, "test")
    block = page.provenance["blocks"][0]
    block.update(
        text=figure,
        text_span=[len(marker), len(marker) + len(figure)],
        block_span=[0, len(figure)],
        context_id="figure:long",
        parent_id="figure:long",
        locations=[{"page": 5, "charspan": [0, len(figure)], "bbox": None}],
    )
    removed = deepcopy(block)
    removed.update(
        block_id="removed",
        text="AUDIT_ONLY_SECRET_DO_NOT_EXPOSE",
        text_span=None,
        block_span=None,
        mapping="coarse",
    )
    page.provenance["blocks"].append(removed)
    chunks = chunk_document(ParsedDocument("doc.pdf", [page]), 35, 4)
    middle = chunks[len(chunks) // 2]
    assert "[Figure text" not in middle.text and "[End figure text]" not in middle.text
    assert middle.source_context[0]["context_id"] == "figure:long"
    assert all("text" not in block for block in middle.source_context)
    prompt = (
        _templates()
        .get_template("mock_prompt.j2")
        .render(course="Synthetic", chunks=[middle], count=1, topic=None)
    )
    assert "figure:long" in prompt
    assert "Spatial relationships are not reconstructed" in prompt
    assert "AUDIT_ONLY_SECRET_DO_NOT_EXPOSE" not in prompt
    assert figure not in prompt  # no unsent surrounding source text was added
    assert middle.text in prompt
