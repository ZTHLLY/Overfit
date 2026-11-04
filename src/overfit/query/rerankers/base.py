"""Provider-neutral reranker contracts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from overfit.models import RetrievedChunk


class RerankStatus(str, Enum):
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RerankDocument:
    index: int
    text: str


@dataclass(frozen=True, slots=True)
class RerankScore:
    index: int
    score: float


@dataclass(frozen=True, slots=True)
class RerankResponse:
    scores: tuple[RerankScore, ...]
    request_id: str | None = None
    provider: str | None = None
    usage: dict | None = None


class Reranker(Protocol):
    provider: str
    model: str

    def rerank(
        self, query: str, documents: Sequence[RerankDocument]
    ) -> RerankResponse: ...


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    selected: tuple[RetrievedChunk, ...]
    drop_reasons: dict[str, str]


@dataclass(frozen=True, slots=True)
class RerankResult:
    status: RerankStatus
    ranked: tuple[RetrievedChunk, ...]
    selected: tuple[RetrievedChunk, ...]
    attempts: int
    latency_ms: float
    run_path: Path | None
    provider: str | None = None
    model: str | None = None
    response: RerankResponse | None = None

    @property
    def success(self) -> bool:
        return self.status is RerankStatus.APPLIED
