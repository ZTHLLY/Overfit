"""Frozen query identities and exact-request cache for rerank evaluation.

This module never constructs a provider client. The explicit live runner is
in :mod:`overfit.evaluation.rerank_runner` and depends on these offline seams.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from overfit.errors import RerankContractError, RerankLogError
from overfit.query.rerankers.base import RerankResponse, RerankScore
from overfit.query.rerankers.logging import safe_endpoint_identity, safe_usage


class RerankEvalTrack(str, Enum):
    QUESTION_ONLY = "question-only"
    QUESTION_WITH_OPTIONS = "question+options"


def build_eval_query(item: dict, track: RerankEvalTrack) -> str:
    """Build one frozen query variant without exposing the answer key."""
    question = item.get("question")
    if not isinstance(question, str) or not question.strip():
        raise RerankContractError("Evaluation item needs a non-empty question.")
    if track is RerankEvalTrack.QUESTION_ONLY:
        return question
    if track is not RerankEvalTrack.QUESTION_WITH_OPTIONS:
        raise RerankContractError("Unknown rerank evaluation track.")
    options = item.get("options")
    if not isinstance(options, dict) or not options:
        raise RerankContractError("Options track needs a non-empty option mapping.")
    lines = []
    for label, value in options.items():
        if not isinstance(label, str) or not isinstance(value, str):
            raise RerankContractError("Evaluation options must be text.")
        lines.append(f"{label}. {value}")
    # Keep this byte-for-byte compatible with the frozen dense baseline.
    # There is deliberately no synthetic heading or answer-key field.
    return f"{question}\n" + "\n".join(lines)


@dataclass(frozen=True, slots=True)
class EvalRerankRequest:
    """Complete non-secret identity of a rerank evaluation request."""

    provider: str
    model: str
    base_url: str
    query: str
    candidate_ids: tuple[str, ...]
    documents: tuple[str, ...]
    timeout: float

    def __post_init__(self) -> None:
        if safe_endpoint_identity(self.base_url) is None:
            raise RerankContractError("Evaluation rerank endpoint must use HTTPS.")
        if (
            len(self.candidate_ids) != len(self.documents)
            or len(set(self.candidate_ids)) != len(self.candidate_ids)
            or any(not isinstance(item, str) or not item for item in self.candidate_ids)
        ):
            raise RerankContractError(
                "Evaluation candidate IDs must uniquely match ordered documents."
            )

    @property
    def digest(self) -> str:
        identity = {
            "protocol": "rerank-eval-cache-v1",
            "provider": self.provider,
            "model": self.model,
            "endpoint": safe_endpoint_identity(self.base_url),
            "query_sha256": _digest(self.query),
            "candidate_ids": list(self.candidate_ids),
            "document_sha256": [_digest(text) for text in self.documents],
            "document_count": len(self.documents),
            "top_n": len(self.documents),
            "timeout": self.timeout,
        }
        encoded = json.dumps(
            identity, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class RerankEvalCache:
    """Atomic cache used only by explicit evaluation paths."""

    def __init__(self, output_dir: Path) -> None:
        self.root = Path(output_dir) / "rerank" / "cache"

    def load(
        self, request: EvalRerankRequest, *, no_cache: bool = False
    ) -> RerankResponse | None:
        if no_cache:
            return None
        path = self.root / f"{request.digest}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError
            if data.get("request_digest") != request.digest:
                return None
            if (
                data.get("endpoint") != safe_endpoint_identity(request.base_url)
                or data.get("candidate_ids") != list(request.candidate_ids)
            ):
                return None
            response = RerankResponse(
                scores=tuple(
                    RerankScore(index=item["index"], score=item["score"])
                    for item in data["scores"]
                ),
                request_id=data.get("request_id"),
                provider=data.get("served_by"),
                usage=data.get("usage"),
            )
            _validate_cached_response(response, len(request.documents))
            return response
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise RerankContractError("Invalid rerank evaluation cache entry.") from None

    def store(
        self, request: EvalRerankRequest, response: RerankResponse
    ) -> Path:
        _validate_cached_response(response, len(request.documents))
        path = self.root / f"{request.digest}.json"
        record = {
            "schema_version": 1,
            "protocol": "rerank-eval-cache-v1",
            "request_digest": request.digest,
            "provider": request.provider,
            "model": request.model,
            "endpoint": safe_endpoint_identity(request.base_url),
            "query_sha256": _digest(request.query),
            "candidate_ids": list(request.candidate_ids),
            "document_sha256": [_digest(text) for text in request.documents],
            "document_count": len(request.documents),
            "timeout": request.timeout,
            "scores": [
                {"index": item.index, "score": item.score}
                for item in response.scores
            ],
            "request_id": response.request_id,
            "served_by": response.provider,
            "usage": safe_usage(response.usage),
        }
        try:
            content = json.dumps(
                record, ensure_ascii=False, sort_keys=True, allow_nan=False
            ) + "\n"
            _atomic_write(path, content)
        except (OSError, TypeError, ValueError):
            raise RerankLogError("Could not persist rerank evaluation cache.") from None
        return path


def _validate_cached_response(response: RerankResponse, count: int) -> None:
    seen: set[int] = set()
    for item in response.scores:
        if (
            type(item.index) is not int
            or item.index < 0
            or item.index >= count
            or item.index in seen
            or isinstance(item.score, bool)
            or not isinstance(item.score, (int, float))
            or not math.isfinite(item.score)
        ):
            raise RerankContractError("Invalid rerank evaluation cache response.")
        seen.add(item.index)
    if seen != set(range(count)):
        raise RerankContractError("Incomplete rerank evaluation cache response.")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
