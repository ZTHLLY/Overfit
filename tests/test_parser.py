"""Layer 2 invariants.

Deliberately short, and meant to stay that way. An invariant earns a test
here when breaking it would be **silent** -- when the command still runs, the
output still looks well-formed, and the damage only surfaces weeks later as
"retrieval is somehow bad". Anything that blows up the first time you run
`overfit inspect` does not need a net underneath it.

That filter is the whole design of this file. Without it a test suite grows
into a second, slower description of the parser, and every future change has
to be made twice. If a case cannot answer "how else would I find out this
broke?", it does not belong here.

There is a second filter, applied by breaking each invariant on purpose and
checking that the matching test goes red. One case did not survive it -- an
assertion that cleaning only ever shrinks its input and settles after one
pass, which stayed green through every plausible way of breaking it. A test
nothing can fail is not a weak test, it is a comment that costs time to run.

Everything below works on synthetic `Page` objects rather than real PDFs. The
cleaning stage is a pure function of `list[Page]`, and the two backends are
thin adapters over pypdf and docling -- whose correctness is theirs to test,
not ours.
"""

from __future__ import annotations

import pytest

from overfit.errors import EmptyExtractionError
from overfit.ingestion import parser
from overfit.models import Page


def test_page_numbers_survive_cleaning():
    """The one thing this layer cannot regenerate.

    Cleaning rebuilds the page list three times over. If it ever drops or
    reorders a page, every citation downstream is wrong by an unknown amount
    while still being perfectly well-formed: real file, plausible page, no
    error anywhere. Nothing else in the pipeline can detect that.

    A page that cleans away to nothing must still hold its slot.
    """
    pages = [
        Page(number=1, text="Real prose on the first page."),
        Page(number=2, text="   \n  \n"),
        Page(number=3, text="More prose on the third."),
    ]

    assert [page.number for page in parser._clean(pages)] == [1, 2, 3]


def test_labelled_structure_is_not_mistaken_for_debris():
    """A recovered structure must not be deleted as a lost one.

    Both halves matter. The debris rule is right to delete a run of short
    unpunctuated lines -- that is a flattened chart. It is wrong to delete
    the same shape when the backend has already said "this is a table", and
    the difference is the label, not the text.

    This is the failure that actually shipped: docling reconstructed a
    two-column table, and cleaning removed it row by row.
    """
    cells = ["Age", "HighRisk", "23", "No", "18", "Yes", "36", "No"]

    labelled = Page(number=1, text="\n".join(cells), structured=tuple(cells))
    assert all(cell in parser._clean([labelled])[0].text for cell in cells)

    # The rule did not get looser, it got informed.
    plain = Page(number=1, text="\n".join(cells))
    assert parser._clean([plain])[0].text.strip() == ""


def test_labelled_furniture_beats_the_frequency_rule():
    """A verdict from the backend outranks a guess about page position.

    Both guards the statistical rule depends on are violated here: only three
    pages, and the footer sits in the middle rather than at an edge. The
    label carries no such preconditions.
    """
    footer = "TEQSA Provider ID PRV12079"
    body = "A real sentence that must survive."
    text = f"Slide title\n{footer}\n{body}"

    labelled = [Page(number=n, text=text, furniture=(footer,)) for n in (1, 2, 3)]
    cleaned = parser._clean(labelled)
    assert all(footer not in page.text for page in cleaned)
    assert all(body in page.text for page in cleaned)

    # Without the label the same pages are left alone, which is also correct:
    # three pages are too few to conclude anything from repetition.
    unlabelled = [Page(number=n, text=text) for n in (1, 2, 3)]
    assert any(footer in page.text for page in parser._clean(unlabelled))


def test_hyphen_break_is_rejoined():
    """Otherwise a word is unsearchable for itself.

    "over-\\nfitting" embeds as two meaningless fragments, and the chunk that
    literally defines overfitting never comes back for the query
    "overfitting" -- which looks exactly like a retrieval problem.
    """
    assert "overfitting" in parser.clean_page(
        "A model that memorises its training data is said to be over-\n"
        "fitting, and generalises poorly."
    )


def test_blank_document_raises_rather_than_indexing_nothing(tmp_path):
    """ "Nothing was extracted" and "there is nothing here" are different.

    A scanned PDF with no text layer must stop the run. Returning an empty
    document instead would put a file in the index that silently contributes
    no chunks, and the user would have no reason to suspect it.
    """
    blank = tmp_path / "scan.txt"
    blank.write_text("   \n\n  \n", encoding="utf-8")

    with pytest.raises(EmptyExtractionError):
        parser.parse(blank)


def _item(text, page, *, ref="b", label="text", parent=None, locations=None):
    from types import SimpleNamespace as NS

    return NS(
        text=text,
        self_ref=ref,
        label=label,
        parent=NS(cref=parent) if parent else None,
        prov=locations
        if locations is not None
        else [NS(page_no=page, charspan=(0, len(text)), bbox=None)],
    )


def _docling_fake(monkeypatch, items, *, count=None, pages=None):
    from types import SimpleNamespace as NS

    calls = []

    class Document:
        def __init__(self):
            self.pages = pages or {}

        def iterate_items(self, *, traverse_pictures=False, with_groups=False):
            calls.append((traverse_pictures, with_groups))
            return iter(
                (item, 0)
                for item in items
                if traverse_pictures or not getattr(item, "parent", None)
            )

    result = NS(document=Document(), input=NS(page_count=count))
    monkeypatch.setattr(
        parser, "_docling_converter", lambda _formula: NS(convert=lambda _: result)
    )
    return calls


def test_docling_keeps_all_locations_and_true_total(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS

    text = "The premise begins on one page and its condition continues on the next."
    locations = [
        NS(
            page_no=2,
            charspan=(0, 35),
            bbox=NS(l=1, t=2, r=3, b=4, coord_origin="TOPLEFT"),
        ),
        NS(page_no=3, charspan=(35, len(text)), bbox=None),
    ]
    _docling_fake(
        monkeypatch, [_item(text, 2, locations=locations)], count=5, pages={2: None}
    )
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"not a real PDF: fake converter only")
    document = parser.parse(path, backend="docling")
    assert [p.number for p in document.pages] == [1, 2, 3, 4, 5]
    assert document.pages[2].text == ""
    assert document.provenance["page_count"] == 5
    block = document.pages[1].provenance["blocks"][0]
    assert [loc["page"] for loc in block["locations"]] == [2, 3]
    assert block["locations"][0]["bbox"]["coord_origin"] == "TOPLEFT"
    assert document.provenance["blocks"][0]["text_span"] is None
    assert document.provenance["blocks"][0]["mapping"] == "coarse"
    import hashlib

    assert (
        document.provenance["document_sha256"]
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert (
        document.pages[1].provenance["document_sha256"]
        == document.provenance["document_sha256"]
    )


def test_docling_partial_pages_never_infer_total(monkeypatch, tmp_path):
    _docling_fake(
        monkeypatch,
        [_item("Partial extraction, known page only.", 17)],
        pages={17: None},
    )
    pages = parser._parse_docling(tmp_path / "not-opened.pdf", formulas=False)
    assert [p.number for p in pages] == [17]
    assert pages[0].provenance["page_count"] is None


def test_picture_children_keep_context_and_are_not_unlabelled_prose(
    monkeypatch, tmp_path
):
    picture = _item("", 7, ref="picture:1", label="picture")
    children = [
        _item(text, 7, ref=f"child:{i}", parent="picture:1")
        for i, text in enumerate(
            ["Relative scale is undefined", "x", "y", "0", "1", "2", "3"]
        )
    ]
    body = _item(
        "The body after the figure remains a separate explanatory sentence.",
        7,
        ref="body",
    )
    calls = _docling_fake(monkeypatch, [picture, *children, body], count=8)
    pages = parser._clean(parser._parse_docling(tmp_path / "fake.pdf", formulas=False))
    page = pages[6]
    assert calls == [(True, True)]
    assert "Relative scale is undefined" in page.text
    assert "[Figure text; spatial relationships not reconstructed]" in page.text
    assert page.text.index("[End figure text]") < page.text.index("The body")
    blocks = {b["block_id"]: b for b in page.provenance["blocks"]}
    assert blocks["child:0"]["context_id"] == "picture:1"
    assert blocks["child:0"]["parent_id"] == "picture:1"
    assert blocks["body"]["context_id"] is None
    assert blocks["picture:1"]["text_span"] is None


def test_docling_table_coordinates_not_confused_with_markdown(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS

    table = _item(
        "",
        3,
        ref="table:1",
        label="table",
        locations=[NS(page_no=3, charspan=(500, 900), bbox=None)],
    )
    table.data = NS(grid=[])
    table.export_to_markdown = lambda: "| Unit | Value |\n| --- | --- |\n| cm | 17 |"
    _docling_fake(monkeypatch, [table], count=4)
    page = parser._clean(parser._parse_docling(tmp_path / "fake.pdf", formulas=False))[
        2
    ]
    assert "| cm | 17 |" in page.text
    block = page.provenance["blocks"][0]
    assert block["mapping"] == "coarse"
    assert block["locations"][0]["charspan"] == [500, 900]


def test_cleaning_degrades_mapping_without_overwriting_original_block():
    original = "A necessary condi-\ntion must hold for this conclusion."
    page = parser._plain_page(2, original, 4, "test")
    cleaned = parser._clean([page])[0]
    block = cleaned.provenance["blocks"][0]
    assert "condition" in cleaned.text
    assert block["text"] == original
    assert block["mapping"] == "coarse"
    assert block["text_span"] == [0, len(cleaned.text)]
    assert block["block_span"] == [0, len(original)]
    assert page.provenance["blocks"][0]["mapping"] == "exact"


def test_pypdf_metadata_uses_reader_total_including_blank_pages(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS

    import pypdf

    monkeypatch.setattr(
        pypdf,
        "PdfReader",
        lambda _: NS(
            is_encrypted=False,
            pages=[
                NS(extract_text=lambda: "A synthetic first page."),
                NS(extract_text=lambda: None),
            ],
        ),
    )
    pages = parser._parse_pdf(tmp_path / "fake.pdf")
    assert [p.number for p in pages] == [1, 2]
    assert pages[1].text == ""
    assert all(p.provenance["page_count"] == 2 for p in pages)


def test_text_file_pages_are_not_claimed_as_physical_pdf_total(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Synthetic prose is not a physically paginated document.")
    document = parser.parse(path)
    assert document.provenance["page_count"] is None
    assert document.provenance["extractor"] == "text/utf-8"


def test_removed_furniture_is_audit_only_not_reintroduced_by_cleaning(
    monkeypatch, tmp_path
):
    footer = _item("Private draft footer", 2, ref="footer", label="page_footer")
    footer.content_layer = "furniture"
    body = _item(
        "A retained proposition with enough material for a complete chunk.",
        2,
        ref="body",
    )
    _docling_fake(monkeypatch, [footer, body], count=3)
    raw = parser._parse_docling(tmp_path / "fake.pdf", formulas=False)
    cleaned = parser._clean(raw)
    blocks = {b["block_id"]: b for b in cleaned[1].provenance["blocks"]}
    assert footer.text not in cleaned[1].text
    assert blocks["footer"]["text"] == footer.text
    assert blocks["footer"]["text_span"] is None
    assert blocks["body"]["mapping"] == "exact"
    assert blocks["body"]["text_span"] == [0, len(body.text)]
    assert raw[1].provenance["blocks"][0]["text_span"] == [0, len(footer.text)]


def test_extractor_locations_beyond_trusted_count_fail(monkeypatch, tmp_path):
    from overfit.errors import ExtractionError

    _docling_fake(monkeypatch, [_item("Impossible physical page.", 7)], count=2)
    with pytest.raises(ExtractionError, match="physical page count"):
        parser._parse_docling(tmp_path / "fake.pdf", formulas=False)


def test_duplicate_blocks_do_not_get_fabricated_clean_alignment():
    page = parser._plain_page(3, "duplicate\nduplicate", 5, "test")
    block = page.provenance["blocks"][0]
    block.update(text="duplicate", text_span=[0, 9], block_span=[0, 9])
    cleaned = parser._replacing(page, "duplicate")
    assert cleaned.provenance["blocks"][0]["text_span"] is None
    assert cleaned.provenance["blocks"][0]["mapping"] == "coarse"


def test_source_changed_during_extraction_never_gets_new_hash_for_old_text(
    monkeypatch, tmp_path
):
    from overfit.errors import ExtractionError

    path = tmp_path / "changing.txt"
    path.write_text("Original source text, before extraction.")

    def changing_reader(source, backend):
        old = parser._plain_page(1, source.read_text(), None, "synthetic")
        source.write_text("Different source text, changed during extraction.")
        return [old]

    monkeypatch.setattr(parser, "_read", changing_reader)
    with pytest.raises(ExtractionError, match="source changed during extraction"):
        parser.parse(path)


def test_explicit_picture_caption_refs_preserve_nonancestor_relationships(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace as NS

    first = _item("", 3, ref="figure:a", label="picture")
    second = _item("", 3, ref="figure:b", label="picture")
    first.captions = [NS(cref="caption:shared")]
    second.captions = [NS(cref="caption:shared")]
    caption = _item(
        "An explicit shared caption describes both identified plots.",
        4,
        ref="caption:shared",
        label="caption",
        parent="body",
    )
    unrelated = _item(
        "A nearby caption without an explicit relationship.",
        4,
        ref="caption:unrelated",
        label="caption",
        parent="body",
    )
    _docling_fake(monkeypatch, [first, second, caption, unrelated], count=5)
    pages = parser._clean(parser._parse_docling(tmp_path / "fake.pdf", formulas=False))
    blocks = pages[3].provenance["blocks"]
    assert {b["context_id"] for b in blocks if b["block_id"] == "caption:shared"} == {
        "figure:a",
        "figure:b",
    }
    assert all(b["parent_id"] == "body" for b in blocks)
    assert (
        next(b for b in blocks if b["block_id"] == "caption:unrelated")["context_id"]
        is None
    )
    assert pages[3].text.count(caption.text) == 1
