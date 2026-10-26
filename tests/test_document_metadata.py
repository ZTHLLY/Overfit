"""Strict source metadata is separate from the text actually used as evidence."""

from copy import deepcopy

import pytest

from overfit.models import Chunk, Page, ParsedDocument
from overfit.provenance import provenance_pages, validate_provenance


def metadata(text="Original evidence."):
    return {
        "schema_version": 1,
        "document_sha256": "a" * 64,
        "page_count": 9,
        "extractor": "synthetic/1",
        "blocks": [
            {
                "block_id": "b1",
                "label": "text",
                "parent_id": None,
                "context_id": None,
                "text": text,
                "locations": [{"page": 4, "charspan": [0, len(text)], "bbox": None}],
                "text_span": [0, len(text)],
                "block_span": [0, len(text)],
                "mapping": "exact",
            }
        ],
    }


def test_old_constructors_and_deep_copy():
    assert Page(1, "old").provenance is None
    assert ParsedDocument("old", []).provenance is None
    assert Chunk("c", "old", "old", 1).provenance is None
    value = metadata()
    page = Page(4, "Original evidence.", provenance=value)
    value["blocks"][0]["locations"][0]["page"] = 8
    assert provenance_pages(page.provenance) == [4]
    assert validate_provenance(None) is None


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("schema_version",), 2),
        (("document_sha256",), "z" * 64),
        (("page_count",), True),
        (("page_count",), 0),
        (("extractor",), "\nunsafe"),
        (("blocks",), ()),
        (("blocks", 0, "label"), ""),
        (("blocks", 0, "parent_id"), ""),
        (("blocks", 0, "text"), None),
        (("blocks", 0, "text_span"), [True, 2]),
        (("blocks", 0, "text_span"), (0, 2)),
        (("blocks", 0, "text_span"), [2, 0]),
        (("blocks", 0, "block_span"), [0, 999]),
        (("blocks", 0, "text_span"), None),
        (("blocks", 0, "locations", 0, "page"), 10),
        (("blocks", 0, "locations", 0, "page"), 1.0),
        (("blocks", 0, "locations", 0, "charspan"), [-1, 0]),
        (("blocks", 0, "mapping"), "guessed"),
    ],
)
def test_reject_malformed_contract(path, value):
    obj = metadata()
    target = obj
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_provenance(obj, text="Original evidence.")


@pytest.mark.parametrize("path", [(), ("blocks", 0), ("blocks", 0, "locations", 0)])
def test_missing_and_unknown_keys_rejected(path):
    for action in ("missing", "unknown"):
        obj = metadata()
        target = obj
        for key in path:
            target = target[key]
        if action == "missing":
            target.pop(next(iter(target)))
        else:
            target["surprise"] = None
        with pytest.raises(ValueError):
            validate_provenance(obj)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), True])
def test_bbox_finite_nonboolean(number):
    value = metadata()
    value["blocks"][0]["locations"][0]["bbox"] = {
        "l": number,
        "t": 0,
        "r": 2,
        "b": 3,
        "coord_origin": "BOTTOMLEFT",
    }
    with pytest.raises(ValueError):
        validate_provenance(value)


def test_exact_requires_current_text_and_document_has_no_text_coordinates():
    value = metadata()
    with pytest.raises(ValueError):
        Page(4, "Altered evidence.", provenance=value)
    with pytest.raises(ValueError):
        Chunk("c", "Original", "file", 4, provenance=value)
    with pytest.raises(ValueError):
        ParsedDocument("file", [], provenance=value)
    value["blocks"][0].update(text_span=None, mapping="coarse")
    assert ParsedDocument("file", [], value).provenance == value


def test_original_extractor_charspan_has_its_own_coordinate_domain():
    value = metadata("| markdown table |")
    value["blocks"][0]["locations"][0]["charspan"] = [800, 900]
    value["blocks"][0]["mapping"] = "coarse"
    assert validate_provenance(value, text="| markdown table |") == deepcopy(value)
