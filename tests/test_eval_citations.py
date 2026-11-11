"""Strict membership never repairs citations or fills interval holes."""

import pytest

from overfit.evaluation.citations import check_citation


def chunk(identifier="c1", source="week1/a.pdf", page=2, end=None):
    return {"id": identifier, "source": source, "page": page, "page_end": end}


@pytest.mark.parametrize("page", [2, 3, 4])
def test_exact_closed_interval(page):
    assert check_citation({"source": "week1/a.pdf", "page": page}, [chunk(end=4)]) == {
        "status": "valid",
        "chunk_ids": ["c1"],
    }


@pytest.mark.parametrize("page", [1, 5, 8])
def test_no_near_page_repair_or_joining_interval_holes(page):
    assert check_citation(
        {"source": "week1/a.pdf", "page": page},
        [chunk(end=4), chunk("c2", page=9, end=10)],
    ) == {"status": "page_not_supplied", "chunk_ids": []}


@pytest.mark.parametrize("source", ["a.pdf", "week2/a.pdf", "Week1/a.pdf", None])
def test_source_is_exact_not_basename_or_casefold(source):
    assert (
        check_citation({"source": source, "page": 2}, [chunk()])["status"]
        == "unknown_source"
    )


@pytest.mark.parametrize("page", [None, 0, -1, True, False, "2", 2.0, [], {}])
def test_invalid_page_is_not_coerced(page):
    assert check_citation({"source": "week1/a.pdf", "page": page}, [chunk()]) == {
        "status": "invalid_page",
        "chunk_ids": [],
    }


def test_missing_page_and_empty_material():
    assert check_citation({"source": "week1/a.pdf"}, [])["status"] == "invalid_page"
    assert (
        check_citation({"source": "week1/a.pdf", "page": 2}, [])["status"]
        == "unknown_source"
    )


def test_overlap_deduplicates_chunk_ids_and_does_not_mutate_input():
    item = {"source": "week1/a.pdf", "page": 2}
    material = [chunk(), chunk(), chunk("c2", page=1, end=3)]
    assert check_citation(item, material) == {
        "status": "valid",
        "chunk_ids": ["c1", "c2"],
    }
    assert item == {"source": "week1/a.pdf", "page": 2}
    assert len(material) == 3


@pytest.mark.parametrize(
    "bad", [chunk(page=0), chunk(page=True), chunk(end=1), chunk(end="3")]
)
def test_invalid_material_is_rejected_not_silently_repaired(bad):
    with pytest.raises(ValueError, match="Invalid material"):
        check_citation({"source": "week1/a.pdf", "page": 2}, [bad])


@pytest.mark.parametrize("bad", [None, [], ["chunk"], "chunk", 1, True])
def test_non_dictionary_material_raises_value_error(bad):
    with pytest.raises(ValueError, match="Invalid material record"):
        check_citation({"source": "week1/a.pdf", "page": 2}, [bad])
