"""New policy is location diagnostics, not programmatic semantic approval."""

import copy
import json
from types import SimpleNamespace

import pytest

from overfit.evaluation import judge
from overfit.evaluation.elastic import citation_window, locate_citation, source_labels
from tests.provenance_samples import provenance_for


def chunk(*, pages=(47,), total=80, mapping="exact"):
    text = "A false premise does not imply this conclusion."
    return {"id": "c", "source": "heterogeneous.pdf", "page": min(pages),
            "page_end": max(pages), "section": None, "text": text,
            "provenance": provenance_for(text, pages=pages, total=total, mapping=mapping)}


def item(page=47):
    return {"source": "heterogeneous.pdf", "page": page, "question": "Why?", "answer": "Because."}


def review(verdict="supported", quote=None):
    return {"material_support": "supported", "citation_support": verdict, "reason": "fixture",
            "claims": [{"text": "Claim", "verdict": "supported", "evidence": [{"chunk_id": "c"}]}],
            "citation_evidence": [{"chunk_id": "c", "quote": quote}]}


@pytest.mark.parametrize("page,expected", [(45, "source_issue"), (46, "verified"),
                                           (47, "verified"), (48, "verified"), (49, "source_issue")])
def test_fixed_physical_window_positive_and_negative(page, expected):
    material = chunk(pages=(page,))
    result = locate_citation(item(), [material], material)
    assert result["status"] == expected
    assert result["window"] == [46, 48] and result["anchor"]["page"] == 47
    assert "verdict" not in result


@pytest.mark.parametrize("anchor,total,window", [(1, 1, [1, 1]), (1, 70, [1, 2]), (70, 70, [69, 70])])
def test_true_page_count_clips_ends(anchor, total, window):
    material = chunk(pages=(anchor,), total=total)
    assert citation_window(item(anchor), [material])["window"] == window


@pytest.mark.parametrize("field", ["document_sha256", "page_count"])
def test_missing_metadata_does_not_fetch_or_trigger_human(field):
    material = chunk()
    material["provenance"][field] = None
    result = judge.validate_review("support", json.dumps(review()), item(), [material])
    assert result["human_review"]["status"] == "not_required"
    assert result["verification"]["elastic_citation"]["status"] == "unverified"
    assert result["model_review"] == review()


@pytest.mark.parametrize("page", [0, -1, True, 81])
def test_invalid_anchor_not_corrected(page):
    result = citation_window(item(page), [chunk()])
    assert result["code"] == "citation_invalid_anchor"
    assert result["anchor"]["page"] == page and result["window"] is None


@pytest.mark.parametrize("field,value", [("document_sha256", "b" * 64), ("page_count", 81)])
def test_source_identity_conflict_cannot_pick_favorable_version(field, value):
    materials = [chunk(), chunk()]
    materials[1]["id"] = "other"
    materials[1]["provenance"][field] = value
    result = judge.validate_review("support", json.dumps(review()), item(), materials)
    assert {"code": "citation_identity_conflict", "path": "citation_evidence[0]"} in result["human_review"]["reasons"]
    assert result["model_review"]["citation_support"] == "supported"


@pytest.mark.parametrize("mapping", ["exact", "coarse"])
def test_multipage_known_inside_window_does_not_require_exact_quote(mapping):
    material = chunk(pages=(47, 48), mapping=mapping)
    result = judge.validate_review("support", json.dumps(review()), item(), [material])
    record = result["verification"]["evidence_records"][-1]
    assert record["elastic_location"]["status"] == "verified"
    assert result["human_review"]["status"] == "not_required"
    assert record["quote_status"] == "quote_not_provided"


def test_giant_intersection_is_not_verified_or_semantically_rejected():
    material = chunk(pages=(1, 47, 80))
    result = judge.validate_review("support", json.dumps(review()), item(), [material])
    record = result["verification"]["evidence_records"][-1]
    assert record["elastic_location"]["status"] == "unverified"
    assert result["human_review"]["status"] == "not_required"
    assert result["model_review"] == review()


def test_audit_only_blocks_do_not_contribute_window_evidence_or_request_text():
    material = chunk()
    block = material["provenance"]["blocks"][0]
    block.update(text="AUDIT_SECRET_NOT_SENT", text_span=None, block_span=None, mapping="coarse")
    result = locate_citation(item(), [material], material)
    assert result["status"] == "unverified" and result["evidence_pages"] == []
    settings = SimpleNamespace(model="fake", temperature=0, timeout=1, max_output_tokens=4000)
    for phase in ("answerability", "support"):
        request = judge.build_request(phase, item(), [material], settings)
        assert "AUDIT_SECRET_NOT_SENT" not in json.dumps(request)
    assert source_labels(material)["blocks"] == []


def test_incomplete_mapping_cannot_claim_verified_window():
    material = chunk()
    material["provenance"]["blocks"][0].update(text_span=[0, 1], block_span=[0, 1])
    assert locate_citation(item(), [material], material)["status"] == "unverified"


def test_exact_quote_can_narrow_to_actual_mapped_block_without_widening_window():
    material = chunk()
    material["text"] = "INSIDE evidence. OUTSIDE evidence."
    blocks = []
    for name, a, b, page in [("inside", 0, 16, 48), ("outside", 17, 34, 60)]:
        block = provenance_for(material["text"][a:b], pages=(page,))["blocks"][0]
        block.update(block_id=name, text_span=[a, b])
        blocks.append(block)
    material["provenance"]["blocks"] = blocks
    material.update(page=48, page_end=60)
    request = review(quote="INSIDE evidence.")
    result = judge.validate_review("support", json.dumps(request), item(), [material])
    located = result["verification"]["evidence_records"][-1]["elastic_location"]
    assert located["status"] == "verified" and located["window"] == [46, 48]
    request = review(quote="OUTSIDE evidence.")
    result = judge.validate_review("support", json.dumps(request), item(), [material])
    assert result["human_review"]["reasons"] == [{"code": "citation_outside_window", "path": "citation_evidence[0]"}]
    assert result["model_review"] == request


@pytest.mark.parametrize("verdict", ["partially_supported", "unknown"])
def test_legal_location_does_not_hide_model_uncertainty(verdict):
    material = chunk()
    value = review(verdict)
    result = judge.validate_review("support", json.dumps(value), item(), [material])
    assert result["model_review"] == value
    assert result["human_review"]["status"] == "pending"


def test_wrong_file_is_not_rescued_by_same_page_or_metadata():
    material = chunk()
    wrong = item() | {"source": "different.pdf"}
    result = judge.validate_review("support", json.dumps(review()), wrong, [material])
    assert result["human_review"]["status"] == "pending"


def test_anchor_origin_and_original_context_are_explicit_and_no_mutation():
    material = chunk()
    before = copy.deepcopy(material)
    result = judge.validate_review("support", json.dumps(review()), item(), [material], anchor_origin="challenge_input")
    assert result["verification"]["elastic_citation"]["anchor_origin"] == "challenge_input"
    assert result["verification"]["context_scope"] == "original_generation_context"
    assert result["verification"]["supplemental_review_context"] is None
    assert material == before


def test_figure_carrier_context_labels_survive_without_audit_body():
    material = chunk()
    carrier = material["provenance"]["blocks"][0]
    carrier.update(label="picture", parent_id="page-parent", context_id="figure-7",
                   text="AUDIT_PREFIX " + material["text"], block_span=[13, 13 + len(material["text"])])
    labels = source_labels(material)
    assert labels["blocks"][0]["context_id"] == "figure-7"
    assert labels["blocks"][0]["parent_id"] == "page-parent"
    assert labels["blocks"][0]["label"] == "picture"
    assert "AUDIT_PREFIX" not in json.dumps(labels)
    settings = SimpleNamespace(model="fake", temperature=0, timeout=1, max_output_tokens=4000)
    request = judge.build_request("support", item(), [material], settings)
    assert "figure-internal" in request["messages"][0]["content"]
    assert "Spatial" in request["messages"][0]["content"]
    assert "AUDIT_PREFIX" not in json.dumps(request)


def test_empty_carrier_mapping_is_not_extra_model_context():
    material = chunk()
    material["provenance"]["blocks"][0].update(text_span=[0, 0], block_span=[0, 0])
    assert source_labels(material)["blocks"] == []
    assert locate_citation(item(), [material], material)["status"] == "unverified"
