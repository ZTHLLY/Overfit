"""Layer 6 -- Retriever.

Small, because the two things it needs already exist: `embedding.embed`
turns the question into a vector, and `store.search` finds the nearest
chunks. What this layer contributes is the guarantee that the query is
embedded by *the same model* that built the index -- which it gets for free
by calling the same function layer 4 calls, rather than by remembering to.

This is also where retrieval quality will be improved later. A re-ranker
belongs inside `retrieve`, not beside it: the contract is "a question in,
the best chunks out", and how that is achieved is nobody else's business.
The `fetch_k` parameter is already threaded through for exactly that -- a
re-ranker needs a wider candidate pool to reorder.
"""

from __future__ import annotations

from overfit.embedding import Embedder
from overfit.models import Chunk, RetrievedChunk
from overfit.query import selection
from overfit.storage.store import VectorStore

__all__ = ["retrieve", "gather_material", "rank_topics"]

# Cap on how finely a course is divided. Past this the clusters stop being
# topics and start being paragraphs, and weight loses its meaning.
_MAX_TOPICS = 12


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
) -> list[RetrievedChunk]:
    """Find the chunks most relevant to `query`.

    Bear in mind what this can and cannot do. Vector search measures whether
    two passages are *about the same thing*, not whether one *answers* the
    other -- so a question about overfitting will happily surface a
    definition of underfitting, which is its near neighbour in embedding
    space and its opposite in meaning. Nothing here fixes that; a re-ranker
    reading question and chunk together is what fixes it.
    """
    query_vector = embedder.embed_one(query)
    return store.search(query_vector, top_k=top_k, fetch_k=fetch_k)


def gather_material(
    store: VectorStore,
    embedder: Embedder,
    *,
    count: int,
    topic: str | None = None,
    pool: int = 40,
    diversity: float = 0.4,
) -> list[Chunk]:
    """Collect the material a generation command should work from.

    Two modes, one purpose -- material that covers ground rather than
    circling one spot:

    * No topic: sample the whole course for spread, so an exam touches many
      parts of the unit instead of whichever part happens to be largest.
    * With a topic: retrieve a wider pool first, then apply MMR inside it, so
      the questions stay on topic without asking the same thing repeatedly.

    Note this is not `retrieve`. Retrieval finds what matches a question;
    this decides what a whole artifact should be built from. Conflating them
    is why "generate an exam" often produces five variations of one question.
    """
    if topic:
        query_vector = embedder.embed_one(topic)
        hits = store.search(query_vector, top_k=max(pool, count), fetch_k=pool)
        ids = {hit.chunk.id for hit in hits}
        candidates = [item for item in store.all_embedded() if item.chunk.id in ids]
        picked = selection.mmr(candidates, query_vector, count, diversity=diversity)
    else:
        # Weighted by emphasis rather than evenly: every topic that fits gets
        # at least one slot, and the rest go to the subjects the unit keeps
        # returning to across different documents.
        topics = selection.cluster(store.all_embedded(), min(count, _MAX_TOPICS))
        picked = []
        for group, share in selection.allocate(topics, count):
            picked.extend(selection.pick_within(group, share))

    # Present in document order. The model reads material top to bottom, and
    # a sequence that follows the unit is easier to reason over than one
    # ordered by an internal similarity score.
    return sorted(
        (item.chunk for item in picked),
        key=lambda chunk: (chunk.source, chunk.page),
    )
