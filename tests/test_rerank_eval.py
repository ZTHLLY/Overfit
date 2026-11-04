"""Offline seams for the later dual-track rerank evaluation."""

from __future__ import annotations

import json

import pytest

from overfit.errors import RerankContractError
from overfit.evaluation.rerank import (
    EvalRerankRequest,
    RerankEvalCache,
    RerankEvalTrack,
    build_eval_query,
)
from overfit.query.rerankers.base import RerankResponse, RerankScore


def _item() -> dict:
    return {
        "id": "q1",
        "question": "Which statement is correct?",
        "options": {"A": "First", "B": "Second"},
        "correct_option": "B",
    }


def _request(
    *,
    query="question",
    model="voyageai/rerank-3",
    text="alpha",
    base_url="https://openrouter.ai/api/v1",
    candidate_ids=("c1", "c2"),
):
    return EvalRerankRequest(
        provider="openrouter",
        model=model,
        base_url=base_url,
        query=query,
        candidate_ids=candidate_ids,
        documents=(text, "beta"),
        timeout=30.0,
    )


def test_eval_queries_keep_question_only_and_question_with_options_separate():
    question_only = build_eval_query(_item(), RerankEvalTrack.QUESTION_ONLY)
    with_options = build_eval_query(_item(), RerankEvalTrack.QUESTION_WITH_OPTIONS)

    assert question_only == "Which statement is correct?"
    assert with_options == (
        "Which statement is correct?\nA. First\nB. Second"
    )
    assert "correct_option" not in with_options
    assert "B" in with_options  # all option labels are query input, not the answer key


def test_eval_cache_requires_exact_request_identity_and_supports_no_cache(tmp_path):
    cache = RerankEvalCache(tmp_path)
    request = _request()
    response = RerankResponse(
        scores=(RerankScore(1, 0.9), RerankScore(0, 0.4)),
        request_id="request-1",
        provider="VoyageAI",
        usage={"total_tokens": 12},
    )

    path = cache.store(request, response)

    assert cache.load(request) == response
    assert cache.load(request, no_cache=True) is None
    assert cache.load(_request(query="different")) is None
    assert cache.load(_request(model="different")) is None
    assert cache.load(_request(text="changed")) is None
    assert cache.load(_request(base_url="https://other.example/v1")) is None
    assert cache.load(_request(candidate_ids=("c2", "c1"))) is None
    assert path.parent == tmp_path / "rerank" / "cache"


def test_eval_cache_contains_hashes_not_documents_or_secrets(tmp_path):
    cache = RerankEvalCache(tmp_path)
    request = _request(
        query="private query",
        text="private document",
        base_url="https://user:SECRET@example.com/v1?token=SECRET",
    )
    path = cache.store(
        request,
        RerankResponse(
            scores=(RerankScore(0, 0.8), RerankScore(1, 0.2)),
            usage={"total_tokens": 12, "debug": "SECRET-RESPONSE"},
        ),
    )

    data = json.loads(path.read_text())
    raw = path.read_text()
    assert data["request_digest"] == request.digest
    assert data["document_count"] == 2
    assert data["endpoint"] == "https://example.com/v1"
    assert data["candidate_ids"] == ["c1", "c2"]
    assert "private query" not in raw
    assert "private document" not in raw
    assert "api_key" not in raw
    assert "SECRET-RESPONSE" not in raw


@pytest.mark.parametrize("invalid_json", ["[]", '"scalar"', "null"])
def test_eval_cache_non_object_entry_is_a_contract_error(tmp_path, invalid_json):
    cache = RerankEvalCache(tmp_path)
    request = _request()
    path = tmp_path / "rerank" / "cache" / f"{request.digest}.json"
    path.parent.mkdir(parents=True)
    path.write_text(invalid_json)

    with pytest.raises(RerankContractError, match="cache entry"):
        cache.load(request)
