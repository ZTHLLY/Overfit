"""Retriever integration: query routes rerank, no-query routes clustering."""

from __future__ import annotations

import json
import math

from overfit.models import Chunk, EmbeddedChunk, RetrievedChunk
from overfit.query import retriever
from overfit.query.rerankers.base import RerankResponse, RerankScore
from overfit.query.rerankers.service import RerankService


class Embedder:
    def embed_one(self, query):
        assert query
        return [1.0, 0.0]


class Store:
    def __init__(self, embedded):
        self.embedded = embedded
        self.limits = []

    def search_candidates(self, vector, *, limit):
        self.limits.append(limit)
        return [
            RetrievedChunk(item.chunk, score, rank)
            for rank, (item, score) in enumerate(
                zip(self.embedded, [0.99 - i * 0.01 for i in range(len(self.embedded))]),
                1,
            )
        ][:limit]

    def all_embedded(self):
        return self.embedded


class ReverseAdapter:
    provider = "fixture"
    model = "fixture"

    def __init__(self):
        self.documents = []

    def rerank(self, query, documents):
        self.documents.append(tuple(documents))
        size = len(documents)
        return RerankResponse(
            tuple(RerankScore(index=i, score=float(i)) for i in range(size))
        )


def _embedded(index, *, text=None, source=None, vector=None):
    return EmbeddedChunk(
        Chunk(
            f"c{index}",
            text or (f"Substantial document {index}. " * 20),
            source or f"source-{index}.pdf",
            index + 1,
        ),
        vector or [1.0, 0.0],
    )


def test_search_fetches_up_to_40_reranks_all_and_returns_10(tmp_path):
    store = Store([_embedded(i) for i in range(12)])
    adapter = ReverseAdapter()
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)
    seen = []

    hits = retriever.retrieve(
        "query",
        store,
        Embedder(),
        top_k=10,
        fetch_k=40,
        reranker=service,
        on_rerank=seen.append,
    )

    assert store.limits == [40]
    assert len(adapter.documents[0]) == 12
    assert [hit.chunk.id for hit in hits] == [f"c{i}" for i in range(11, 1, -1)]
    assert seen[0].success and seen[0].selected == tuple(hits)
    record = json.loads(next((tmp_path / "rerank" / "runs").glob("*.json")).read_text())
    assert record["candidate_budget"] == 40
    assert record["actual_candidate_count"] == 12
    assert record["final_budget"] == 10
    assert record["selection_policy"] == {
        "applied": False,
        "near_duplicate_threshold": None,
        "source_cap": None,
    }


def test_product_search_never_expands_candidate_pool_past_40(tmp_path):
    store = Store([_embedded(i) for i in range(45)])
    adapter = ReverseAdapter()
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    hits = retriever.retrieve(
        "query",
        store,
        Embedder(),
        top_k=10,
        fetch_k=80,
        reranker=service,
    )

    assert store.limits == [40]
    assert len(adapter.documents[0]) == 40
    assert len(hits) == 10


def test_topic_filters_then_reranks_and_applies_diversity(tmp_path):
    duplicate = "Duplicate explanation with enough substance. " * 10
    embedded = [
        _embedded(0, text="title"),
        _embedded(1, text=duplicate, source="a.pdf", vector=[1.0, 0.0]),
        _embedded(2, text=duplicate, source="b.pdf", vector=[1.0, 0.0]),
        _embedded(3, source="a.pdf", vector=[0.0, 1.0]),
        _embedded(4, source="a.pdf", vector=[0.0, -1.0]),
        _embedded(5, source="a.pdf", vector=[-1.0, 0.0]),
        _embedded(6, source="c.pdf", vector=[0.7, 0.7]),
    ]
    store = Store(embedded)
    adapter = ReverseAdapter()
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    chunks = retriever.gather_material(
        store,
        Embedder(),
        count=10,
        topic="topic query",
        pool=40,
        reranker=service,
    )

    assert store.limits == [40]
    assert len(adapter.documents[0]) == 6  # short c0 was not sent
    assert [chunk.id for chunk in chunks] == ["c6", "c5", "c4", "c2"]
    assert sum(chunk.source == "a.pdf" for chunk in chunks) == 2
    assert not ({"c1", "c2"} <= {chunk.id for chunk in chunks})
    record_text = next((tmp_path / "rerank" / "runs").glob("*.json")).read_text()
    record = json.loads(record_text)
    assert record["candidate_budget"] == 40
    assert record["actual_candidate_count"] == 6
    assert record["final_budget"] == 10
    assert record["selection_policy"] == {
        "applied": True,
        "near_duplicate_threshold": 0.92,
        "source_cap": 2,
    }
    assert "low_information" in record_text
    assert "exact_duplicate" in record_text
    assert "source_cap" in record_text


def test_topic_filters_long_title_only_chunk_before_provider(tmp_path):
    title = "ADVANCED MACHINE LEARNING " * 12
    prose = "This is a complete explanatory sentence about a concept. " * 8
    store = Store([_embedded(0, text=title), _embedded(1, text=prose)])
    adapter = ReverseAdapter()
    service = RerankService(adapter, output_dir=tmp_path, sleep=lambda _: None)

    chunks = retriever.gather_material(
        store,
        Embedder(),
        count=10,
        topic="topic query",
        reranker=service,
    )

    assert [item.text for item in adapter.documents[0]] == [prose]
    assert [chunk.id for chunk in chunks] == ["c1"]


def test_topic_near_duplicate_threshold_is_inclusive_at_point_92(tmp_path):
    store = Store(
        [
            _embedded(0, vector=[1.0, 0.0]),
            _embedded(1, vector=[0.92, math.sqrt(1 - 0.92**2)]),
        ]
    )
    service = RerankService(
        ReverseAdapter(), output_dir=tmp_path, sleep=lambda _: None
    )

    chunks = retriever.gather_material(
        store,
        Embedder(),
        count=10,
        topic="topic query",
        reranker=service,
    )

    assert [chunk.id for chunk in chunks] == ["c1"]
    record = next((tmp_path / "rerank" / "runs").glob("*.json")).read_text()
    assert "near_duplicate" in record


def test_no_topic_never_uses_reranker_and_keeps_course_selection():
    store = Store([_embedded(0), _embedded(1, vector=[0.0, 1.0])])
    statuses = []

    chunks = retriever.gather_material(
        store,
        Embedder(),
        count=1,
        topic=None,
        reranker=None,
        on_rerank=lambda result: statuses.append(result.status.value),
    )

    assert len(chunks) == 1
    assert store.limits == []
    assert statuses == ["skipped"]

