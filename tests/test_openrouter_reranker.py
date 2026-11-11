"""OpenRouter wire contract with an injected, offline transport."""

from __future__ import annotations

import json

import pytest

from overfit.errors import RerankContractError, RerankExternalError
from overfit.query.rerankers.base import RerankDocument
from overfit.query.rerankers.openrouter import HttpResponse, OpenRouterReranker


def test_openrouter_sends_minimal_payload_and_reads_all_scores():
    requests = []

    def transport(url, headers, body, timeout):
        requests.append((url, headers, json.loads(body), timeout))
        return HttpResponse(
            status=200,
            body=json.dumps(
                {
                    "id": "rerank-1",
                    "provider": "VoyageAI",
                    "results": [
                        {"index": 1, "relevance_score": 0.91, "document": {"text": "ignored"}},
                        {"index": 0, "relevance_score": 0.42, "document": {"text": "ignored"}},
                    ],
                    "usage": {"total_tokens": 25},
                }
            ).encode(),
            headers={"x-request-id": "header-id"},
        )

    adapter = OpenRouterReranker(api_key="SECRET", transport=transport)
    result = adapter.rerank(
        "query",
        [RerankDocument(0, "alpha"), RerankDocument(1, "beta")],
    )

    url, headers, payload, timeout = requests[0]
    assert url == "https://openrouter.ai/api/v1/rerank"
    assert headers["Authorization"] == "Bearer SECRET"
    assert payload == {
        "model": "voyageai/rerank-3",
        "query": "query",
        "documents": ["alpha", "beta"],
        "top_n": 2,
    }
    assert timeout == 30
    assert [(item.index, item.score) for item in result.scores] == [(1, 0.91), (0, 0.42)]
    assert result.request_id == "rerank-1"
    assert result.provider == "VoyageAI"
    assert result.usage == {"total_tokens": 25}


@pytest.mark.parametrize("status", [400, 401, 429, 500, 529])
def test_any_non_2xx_is_external_failure(status):
    adapter = OpenRouterReranker(
        api_key="SECRET",
        transport=lambda *args: HttpResponse(status, b'{"error":"SECRET"}', {}),
    )
    with pytest.raises(RerankExternalError) as caught:
        adapter.rerank("q", [RerankDocument(0, "doc")])
    assert caught.value.status == status
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"{}",
        b'{"results":"wrong"}',
        b'{"results":[{"index":0}]}',
    ],
)
def test_invalid_2xx_body_is_non_retryable_contract_error(body):
    adapter = OpenRouterReranker(
        api_key="SECRET",
        transport=lambda *args: HttpResponse(200, body, {}),
    )
    with pytest.raises(RerankContractError):
        adapter.rerank("q", [RerankDocument(0, "doc")])


def test_transport_exception_is_external_without_secret_leak():
    def fail(*args):
        raise TimeoutError("SECRET timeout")

    adapter = OpenRouterReranker(api_key="SECRET", transport=fail)
    with pytest.raises(RerankExternalError) as caught:
        adapter.rerank("q", [RerankDocument(0, "doc")])
    assert "SECRET" not in str(caught.value)


def test_non_2xx_preserves_non_secret_request_id_for_attempt_log():
    adapter = OpenRouterReranker(
        api_key="SECRET",
        transport=lambda *args: HttpResponse(
            429, b"ignored", {"X-Request-ID": "request-429", "Retry-After": "0.5"}
        ),
    )

    with pytest.raises(RerankExternalError) as caught:
        adapter.rerank("q", [RerankDocument(0, "doc")])

    assert caught.value.request_id == "request-429"
    assert caught.value.retry_after == 0.5

