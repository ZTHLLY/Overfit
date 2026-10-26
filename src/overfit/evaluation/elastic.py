"""Recorded-context-only citation location diagnostics, never semantic verdicts."""

from __future__ import annotations

from copy import deepcopy

from overfit.evaluation.citations import check_citation
from overfit.provenance import validate_provenance

POLICY_VERSION = "citation-window-v1"


def validate_materials(chunks, *, legacy=False):
    if type(chunks) is not list:
        raise ValueError("Materials must be a list")
    check_citation({}, chunks)
    ids = set()
    allowed = {"id", "source", "page", "page_end", "section", "text"}
    for chunk in chunks:
        if (set(chunk) - (allowed if legacy else allowed | {"provenance"})
                or not isinstance(chunk.get("text"), str)
                or not chunk["id"] or chunk["id"] in ids):
            raise ValueError("Invalid material snapshot")
        ids.add(chunk["id"])
        if chunk.get("provenance") is not None:
            validate_provenance(chunk["provenance"], text=chunk["text"])


def source_labels(chunk):
    """Only source labels, never audit-only original block text or captions."""
    value = chunk.get("provenance")
    if value is None:
        return None
    value = validate_provenance(value, text=chunk["text"])
    return {"schema_version": value["schema_version"],
            "document_sha256": value["document_sha256"], "page_count": value["page_count"],
            "blocks": [{key: deepcopy(block[key]) for key in
                        ("block_id", "label", "parent_id", "context_id", "locations", "text_span", "mapping")}
                       for block in value["blocks"] if block["text_span"] is not None
                       and block["text_span"][0] < block["text_span"][1]]}


def citation_window(item, chunks, *, anchor_origin="post_policy"):
    """Freeze the declaration anchor; absence of provenance never fetches sources."""
    if anchor_origin not in {"post_policy", "challenge_input"}:
        raise ValueError("Invalid citation anchor origin")
    validate_materials(chunks)
    result = {"policy_version": POLICY_VERSION, "anchor_origin": anchor_origin,
              "anchor": {"source": item.get("source"), "page": item.get("page")},
              "context_scope": "original_generation_context", "supplemental_review_context": None,
              "document_sha256": None, "page_count": None, "window": None,
              "status": "unverified", "code": "citation_metadata_unknown", "severity": "info"}
    selected = [c for c in chunks if c["source"] == item.get("source")]
    def issue(code):
        result.update(status="source_issue", code=code, severity="review")
        return result
    if not selected:
        return issue("citation_source_mismatch")
    page = item.get("page")
    if type(page) is not int or page < 1:
        return issue("citation_invalid_anchor")
    provenance = [c.get("provenance") for c in selected if c.get("provenance") is not None]
    hashes = {p["document_sha256"].lower() for p in provenance if p["document_sha256"] is not None}
    counts = {p["page_count"] for p in provenance if p["page_count"] is not None}
    if len(hashes) > 1 or len(counts) > 1:
        return issue("citation_identity_conflict")
    result.update(document_sha256=next(iter(hashes), None), page_count=next(iter(counts), None))
    if counts and page > result["page_count"]:
        return issue("citation_invalid_anchor")
    if not hashes or not counts:
        return result
    result.update(window=[max(1, page - 1), min(result["page_count"], page + 1)],
                  status="window_known", code="citation_window_known")
    return result


def locate_citation(item, chunks, chunk, *, start=None, end=None, anchor_origin="post_policy"):
    result = citation_window(item, chunks, anchor_origin=anchor_origin)
    result["evidence_pages"] = []
    if chunk is None:
        result.update(status="source_issue", code="unknown_chunk", severity="review")
        return result
    if chunk["source"] != item.get("source"):
        result.update(status="source_issue", code="citation_source_mismatch", severity="review")
        return result
    if result["status"] != "window_known":
        return result
    provenance = chunk.get("provenance")
    if (provenance is None or (provenance["document_sha256"] or "").lower() != result["document_sha256"]
            or provenance["page_count"] != result["page_count"]):
        result.update(status="unverified", code="citation_metadata_unknown")
        return result
    # Cover every non-whitespace character in the relevant *actual* chunk text.
    # Blocks with no current-text mapping are audit-only and contribute nothing.
    lo, hi = (start, end) if start is not None and end is not None else (0, len(chunk["text"]))
    covered = set()
    pages = set()
    for block in provenance["blocks"]:
        span = block["text_span"]
        if span is None:
            continue
        a, b = max(lo, span[0]), min(hi, span[1])
        if a >= b or not block["locations"]:
            continue
        covered.update(range(a, b))
        pages.update(location["page"] for location in block["locations"])
    result["evidence_pages"] = sorted(pages)
    required = {i for i in range(lo, hi) if not chunk["text"][i].isspace()}
    if not pages or not required or not required <= covered:
        result.update(status="unverified", code="citation_mapping_unverified")
        return result
    left, right = result["window"]
    inside = {page for page in pages if left <= page <= right}
    if inside == pages:
        result.update(status="verified", code="citation_within_window")
    elif not inside:
        result.update(status="source_issue", code="citation_outside_window", severity="review")
    else:
        result.update(status="unverified", code="citation_mapping_unverified")
    return result
