"""Layer 6 -- Retriever.

Small, because the two things it needs already exist: `embedding.embed`
turns the question into a vector, and `store.search` finds the nearest
chunks. What this layer contributes is the guarantee that the query is
embedded by *the same model* that built the index -- which it gets for free
by calling the same function layer 4 calls, rather than by remembering to.

Query retrieval is a two-stage product path: dense search supplies at most
40 candidates, then the configured reranker reads the query and candidate
texts together and returns the final order.  The vector-only helper remains
available solely as an explicit evaluation baseline; it is never a silent
product fallback.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Callable

from overfit.embedding import Embedder
from overfit.errors import RerankConfigurationError
from overfit.models import Chunk, RetrievedChunk
from overfit.query import selection
from overfit.query.rerankers.base import RerankResult, RerankStatus, SelectionDecision
from overfit.query.rerankers.service import RerankService
from overfit.storage.store import VectorStore

__all__ = ["gather_material", "rank_topics", "retrieve", "retrieve_dense"]

# Cap on how finely a course is divided. Past this the clusters stop being
# topics and start being paragraphs, and weight loses its meaning.
_MAX_TOPICS = 12
_NEAR_DUPLICATE_THRESHOLD = 0.92
_SOURCE_CAP = 2


def rank_topics(store: VectorStore, count: int = 12) -> list[selection.Topic]:
    """The course's subjects, heaviest first.

    A useful artifact in its own right: a revision priority list computed
    entirely from the shape of the material, with no model call and nothing
    to hallucinate. Every number in it can be checked against the files.
    """
    return selection.cluster(store.all_embedded(), count)


def retrieve(
    query: str,
    store: VectorStore,
    embedder: Embedder,
    *,
    top_k: int,
    fetch_k: int | None = None,
    reranker: RerankService | None = None,
    on_rerank: Callable[[RerankResult], None] | None = None,
) -> list[RetrievedChunk]:
    """Find the chunks most relevant to `query`.

    Vector search measures whether two passages are *about the same thing*,
    not whether one *answers* the other -- so it supplies a wide candidate
    pool rather than the final result.  The required reranker then reads each
    question/chunk pair and selects the requested final set.
    """
    if reranker is None:
        raise RerankConfigurationError("A reranker is required for query retrieval.")
    query_vector = embedder.embed_one(query)
    candidate_limit = max(0, min(fetch_k if fetch_k is not None else 40, 40))
    candidates = store.search_candidates(query_vector, limit=candidate_limit)
    result = reranker.apply(
        query,
        candidates,
        command="search",
        selector=lambda ranked: _top_selection(ranked, top_k),
        candidate_budget=candidate_limit,
        final_budget=max(top_k, 0),
    )
    if on_rerank is not None:
        on_rerank(result)
    return list(result.selected)


def retrieve_dense(
    query: str,
    store: VectorStore,
    embedder: Embedder,
    *,
    top_k: int,
) -> list[RetrievedChunk]:
    """Explicit vector-only baseline for evaluation, never a product fallback."""
    query_vector = embedder.embed_one(query)
    return store.search(query_vector, top_k=top_k)


def gather_material(
    store: VectorStore,
    embedder: Embedder,
    *,
    count: int,
    topic: str | None = None,
    pool: int = 40,
    diversity: float = 0.4,
    reranker: RerankService | None = None,
    on_rerank: Callable[[RerankResult], None] | None = None,
) -> list[Chunk]:
    """Collect the material a generation command should work from.

    Two modes, one purpose -- material that covers ground rather than
    circling one spot:

    * No topic: sample the whole course for spread, so an exam touches many
      parts of the unit instead of whichever part happens to be largest.
    * With a topic: retrieve up to 40 dense candidates, discard low-information
      passages, rerank the rest, then scan in rerank order while removing exact
      and near duplicates and applying a per-source cap.

    Note this is not `retrieve`. Retrieval finds what matches a question;
    this decides what a whole artifact should be built from. Conflating them
    is why "generate an exam" often produces five variations of one question.
    """
    if topic:
        if reranker is None:
            raise RerankConfigurationError(
                "A reranker is required for topic material selection."
            )
        query_vector = embedder.embed_one(topic)
        candidate_limit = max(0, min(pool, 40))
        hits = store.search_candidates(query_vector, limit=candidate_limit)
        kept = []
        pre_dropped = []
        for hit in hits:
            if _is_low_information(hit.chunk.text):
                pre_dropped.append((hit, "low_information"))
            else:
                kept.append(hit)
        vectors = {item.chunk.id: item.embedding for item in store.all_embedded()}
        result = reranker.apply(
            topic,
            kept,
            command="topic-material",
            selector=lambda ranked: _topic_selection(ranked, vectors, count),
            pre_dropped=pre_dropped,
            candidate_budget=candidate_limit,
            final_budget=max(count, 0),
            near_duplicate_threshold=_NEAR_DUPLICATE_THRESHOLD,
            source_cap=_SOURCE_CAP,
        )
        if on_rerank is not None:
            on_rerank(result)
        return [item.chunk for item in result.selected]
    else:
        if on_rerank is not None:
            on_rerank(
                RerankResult(
                    status=RerankStatus.SKIPPED,
                    ranked=(),
                    selected=(),
                    attempts=0,
                    latency_ms=0.0,
                    run_path=None,
                )
            )
        # Weighted by emphasis rather than evenly: every topic that fits gets
        # at least one slot, and the rest go to the subjects the unit keeps
        # returning to across different documents.
        topics = selection.cluster(store.all_embedded(), min(count, _MAX_TOPICS))
        picked = []
        for group, share in selection.allocate(topics, count):
            picked.extend(selection.pick_within(group, share))

    # The no-query branch retains its historical document order.  Query paths
    # return above in rerank order so the strongest evidence stays first.
    return sorted(
        (item.chunk for item in picked),
        key=lambda chunk: (chunk.source, chunk.page),
    )


def _top_selection(
    ranked: list[RetrievedChunk], count: int
) -> SelectionDecision:
    selected = tuple(ranked[: max(count, 0)])
    return SelectionDecision(
        selected=selected,
        drop_reasons={
            item.chunk.id: "outside_final_limit" for item in ranked[len(selected) :]
        },
    )


def _is_low_information(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 200:
        return True
    # A repeated/all-caps slide heading can exceed the length gate after PDF
    # extraction while still containing no sentence or usable explanation.
    letters = [character for character in stripped if character.isalpha()]
    return (
        "\n" not in stripped
        and not any(mark in stripped for mark in ".?!。！？;:")
        and bool(letters)
        and sum(character.isupper() for character in letters) / len(letters) >= 0.8
    )


def _topic_selection(
    ranked: list[RetrievedChunk],
    vectors: dict[str, list[float]],
    count: int,
) -> SelectionDecision:
    selected: list[RetrievedChunk] = []
    reasons: dict[str, str] = {}
    normalised: set[str] = set()
    source_counts: Counter[str] = Counter()
    for item in ranked:
        if len(selected) >= count:
            reasons[item.chunk.id] = "outside_final_limit"
            continue
        text = " ".join(
            unicodedata.normalize("NFKC", item.chunk.text).casefold().split()
        )
        if text in normalised:
            reasons[item.chunk.id] = "exact_duplicate"
            continue
        vector = vectors.get(item.chunk.id)
        if vector is None:
            raise RerankConfigurationError(
                f"Stored embedding missing for candidate {item.chunk.id}."
            )
        if any(
            selection.similarity(vector, vectors[chosen.chunk.id])
            >= _NEAR_DUPLICATE_THRESHOLD
            for chosen in selected
        ):
            reasons[item.chunk.id] = "near_duplicate"
            continue
        if source_counts[item.chunk.source] >= _SOURCE_CAP:
            reasons[item.chunk.id] = "source_cap"
            continue
        selected.append(item)
        normalised.add(text)
        source_counts[item.chunk.source] += 1
    return SelectionDecision(selected=tuple(selected), drop_reasons=reasons)
