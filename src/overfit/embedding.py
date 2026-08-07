"""Text to vectors -- used by layer 4 (Embedder) and layer 6 (Retriever).

This module sits at the top level rather than inside `ingestion/` for a
reason. Indexing and querying must use the *same* model: a different one is
a different coordinate system, so the distances it produces are meaningless
even though nothing raises. Putting the one function both layers call in a
shared place makes the correct thing the convenient thing, and forking a
second copy visibly wrong.

Any OpenAI-compatible endpoint works -- Ollama, vLLM, LM Studio, a hosted
API -- because the entire provider abstraction is three configuration
values. There is no plugin hierarchy here and none is needed.
"""

from __future__ import annotations

import math
import time
from functools import lru_cache
from typing import Callable, Sequence

from overfit.config import EmbedSettings, get_settings
from overfit.errors import EmbeddingError
from overfit.models import Vector

__all__ = ["Embedder", "get_embedder"]


# Ollama processes a batch serially, so a large batch buys little and risks a
# timeout on slow hardware. Small batches also give the progress callback
# something to report.
_DEFAULT_BATCH = 16

_MAX_ATTEMPTS = 4
_BACKOFF_SECONDS = 1.5

# Text sent for the sole purpose of learning the model's output width.
_PROBE_TEXT = "dimension probe"


class Embedder:
    """Turns text into vectors, with batching, retries and a dimension probe."""

    def __init__(self, settings: EmbedSettings | None = None) -> None:
        self._settings = settings or get_settings().embed
        self._client = None  # built lazily so `--help` stays fast
        self._dim: int | None = None

    # -- properties --------------------------------------------------------

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def base_url(self) -> str:
        return self._settings.base_url

    @property
    def dimension(self) -> int:
        """Vector width, discovered by asking the model rather than by config.

        Deliberately not a settings field. The number is a property of the
        model, and any value a human has to copy correctly will eventually be
        copied wrong -- at which point the store's schema silently disagrees
        with its contents.
        """
        if self._dim is None:
            self._dim = len(self.embed_one(_PROBE_TEXT))
        return self._dim

    # -- main API ----------------------------------------------------------

    def embed(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = _DEFAULT_BATCH,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Vector]:
        """Embed many texts, preserving input order.

        Order preservation is not automatic: the API returns objects tagged
        with an index and is free to reorder them. Getting this wrong pairs
        every vector with the wrong chunk, which produces retrieval that is
        confidently and completely wrong while raising nothing at all.
        """
        if not texts:
            return []

        cleaned = [self._prepare(text) for text in texts]
        vectors: list[Vector] = []

        for start in range(0, len(cleaned), batch_size):
            batch = cleaned[start : start + batch_size]
            vectors.extend(self._embed_batch(batch))
            if on_progress:
                on_progress(min(start + batch_size, len(cleaned)), len(cleaned))

        return vectors

    def embed_one(self, text: str) -> Vector:
        """Embed a single string -- the query side, layer 6."""
        return self.embed([text])[0]

    # -- internals ---------------------------------------------------------

    def _prepare(self, text: str) -> str:
        """Guard against inputs the API rejects.

        An empty string is a legitimate thing to end up with after cleaning,
        but most endpoints return an error for it, failing the whole batch.
        Substituting a space keeps the batch alive and the indices aligned.
        """
        stripped = text.strip()
        return stripped if stripped else " "

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._settings.base_url,
                api_key=self._settings.api_key,
                max_retries=0,  # we do our own, with clearer errors
            )
        return self._client

    def _embed_batch(self, batch: list[str]) -> list[Vector]:
        client = self._ensure_client()
        last_error: Exception | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = client.embeddings.create(model=self.model, input=batch)
            except Exception as exc:  # noqa: BLE001 - SDK raises many types
                last_error = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_SECONDS * (2**attempt))
                continue

            # Sort by the index the server assigned, never by arrival order.
            ordered = sorted(response.data, key=lambda item: item.index)
            if len(ordered) != len(batch):
                raise EmbeddingError(
                    f"asked for {len(batch)} embeddings, received {len(ordered)}",
                    base_url=self.base_url,
                    model=self.model,
                )
            return [_normalise(list(item.embedding)) for item in ordered]

        raise EmbeddingError(
            f"embedding request failed after {_MAX_ATTEMPTS} attempts: {last_error}",
            base_url=self.base_url,
            model=self.model,
        )


def _normalise(vector: Vector) -> Vector:
    """Scale to unit length.

    With unit vectors, cosine similarity is exactly the dot product, so the
    store can use whichever distance function it has and still rank
    identically. It costs one pass at write time and removes a whole class of
    "why does this ranking look odd" question later.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """Shared embedder, so the HTTP client and probed dimension are reused."""
    return Embedder()
