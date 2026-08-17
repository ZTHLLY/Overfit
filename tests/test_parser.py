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
    """"Nothing was extracted" and "there is nothing here" are different.

    A scanned PDF with no text layer must stop the run. Returning an empty
    document instead would put a file in the index that silently contributes
    no chunks, and the user would have no reason to suspect it.
    """
    blank = tmp_path / "scan.txt"
    blank.write_text("   \n\n  \n", encoding="utf-8")

    with pytest.raises(EmptyExtractionError):
        parser.parse(blank)
