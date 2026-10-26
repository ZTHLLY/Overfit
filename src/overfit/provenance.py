"""Strict, versioned source metadata; never a source of extra model text."""

from __future__ import annotations

import math
import re
from copy import deepcopy

_TOP = {"schema_version", "document_sha256", "page_count", "extractor", "blocks"}
_BLOCK = {
    "block_id",
    "label",
    "parent_id",
    "context_id",
    "text",
    "locations",
    "text_span",
    "block_span",
    "mapping",
}


def _object(value, keys):
    if type(value) is not dict or set(value) != keys:
        raise ValueError("Invalid provenance object fields")


def _string(value, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not str or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid provenance identifier")


def _span(value):
    if value is None:
        return
    if (
        type(value) is not list
        or len(value) != 2
        or any(type(n) is not int or n < 0 for n in value)
        or value[0] > value[1]
    ):
        raise ValueError("Invalid provenance span")


def validate_provenance(value, *, text=None) -> dict | None:
    """Validate JSON types and available coordinates, returning an isolated copy.

    With no container text, only structural bounds can be checked. Callers
    binding a Page or Chunk must supply its actual text.
    """
    if value is None:
        return None
    if text is not None and type(text) is not str:
        raise ValueError("Invalid provenance container text")
    _object(value, _TOP)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported provenance schema")
    digest = value["document_sha256"]
    if digest is not None and (
        type(digest) is not str or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
    ):
        raise ValueError("Invalid document SHA-256")
    count = value["page_count"]
    if count is not None and (type(count) is not int or count < 1):
        raise ValueError("Invalid physical page count")
    _string(value["extractor"], nullable=True)
    if type(value["blocks"]) is not list:
        raise ValueError("Invalid provenance blocks")
    for block in value["blocks"]:
        _object(block, _BLOCK)
        for key in ("block_id", "label"):
            _string(block[key])
        for key in ("parent_id", "context_id"):
            _string(block[key], nullable=True)
        if (
            type(block["text"]) is not str
            or type(block["mapping"]) is not str
            or block["mapping"] not in ("exact", "coarse")
        ):
            raise ValueError("Invalid provenance block text/mapping")
        if type(block["locations"]) is not list:
            raise ValueError("Invalid provenance locations")
        for location in block["locations"]:
            _object(location, {"page", "charspan", "bbox"})
            page = location["page"]
            if (
                type(page) is not int
                or page < 1
                or (count is not None and page > count)
            ):
                raise ValueError("Invalid provenance physical page")
            _span(location["charspan"])
            bbox = location["bbox"]
            if bbox is not None:
                _object(bbox, {"l", "t", "r", "b", "coord_origin"})
                _string(bbox["coord_origin"])
                if any(
                    type(bbox[k]) not in (int, float)
                    or (type(bbox[k]) is float and not math.isfinite(bbox[k]))
                    for k in ("l", "t", "r", "b")
                ):
                    raise ValueError("Invalid provenance bbox")
        ts, bs = block["text_span"], block["block_span"]
        _span(ts)
        _span(bs)
        if bs is not None and bs[1] > len(block["text"]):
            raise ValueError("Block span exceeds original text")
        if ts is not None and text is not None and ts[1] > len(text):
            raise ValueError("Text span exceeds container text")
        if block["mapping"] == "exact":
            if ts is None or bs is None:
                raise ValueError("Exact provenance requires both spans")
            if ts[1] - ts[0] != bs[1] - bs[0]:
                raise ValueError("Exact provenance spans differ in length")
            if text is not None and text[ts[0] : ts[1]] != block["text"][bs[0] : bs[1]]:
                raise ValueError("Exact provenance text differs")
    return deepcopy(value)


def provenance_pages(value) -> list[int]:
    """Known physical locations, not a claim of semantic support."""
    value = validate_provenance(value)
    return (
        sorted({loc["page"] for block in value["blocks"] for loc in block["locations"]})
        if value
        else []
    )
