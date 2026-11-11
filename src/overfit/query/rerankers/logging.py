"""Atomic, fail-closed JSON recording for rerank runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from overfit.errors import RerankLogError
from overfit.models import RetrievedChunk


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def safe_endpoint_identity(value: str | None) -> str | None:
    """Return only the non-secret HTTPS origin/path identity."""
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"


@dataclass(slots=True)
class RerankRunLogger:
    root: Path
    run_path: Path
    data: dict

    @classmethod
    def create(
        cls,
        output_dir: Path,
        *,
        command: str,
        query: str,
        candidates: list[RetrievedChunk],
        pre_dropped: list[tuple[RetrievedChunk, str]],
        provider: str,
        model: str,
        timeout: float,
        candidate_budget: int,
        final_budget: int,
        near_duplicate_threshold: float | None,
        source_cap: int | None,
        endpoint: str | None = None,
    ) -> RerankRunLogger:
        try:
            root = Path(output_dir) / "rerank"
            runs = root / "runs"
            runs.mkdir(parents=True, exist_ok=True, mode=0o700)
            run_id = str(uuid4())
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = runs / f"{stamp}-{command}-{run_id}.json"
            data = {
                "schema_version": 1,
                "protocol": "rerank-v1",
                "run_id": run_id,
                "command": command,
                "started_at": _now(),
                "finished_at": None,
                "status": "started",
                "provider": provider,
                "model": model,
                "endpoint": safe_endpoint_identity(endpoint),
                "timeout": timeout,
                "candidate_budget": candidate_budget,
                "actual_candidate_count": len(candidates),
                "final_budget": final_budget,
                "selection_policy": {
                    "applied": (
                        near_duplicate_threshold is not None or source_cap is not None
                    ),
                    "near_duplicate_threshold": near_duplicate_threshold,
                    "source_cap": source_cap,
                },
                "query": query,
                "query_sha256": _digest(query),
                "attempts": [],
                "candidates": [
                    _candidate(item, text_hash=_digest(item.chunk.text))
                    for item in candidates
                ],
                "pre_dropped": [
                    {
                        **_candidate(item, text_hash=_digest(item.chunk.text)),
                        "drop_reason": reason,
                    }
                    for item, reason in pre_dropped
                ],
                "usage": None,
                "request_id": None,
                "served_by": None,
                "latency_ms": None,
            }
            logger = cls(root=root, run_path=path, data=data)
            logger._persist()
            return logger
        except Exception as exc:
            if isinstance(exc, RerankLogError):
                raise
            raise RerankLogError("Could not initialize rerank recording.") from None

    def start_attempt(self, number: int, *, started_at: str) -> None:
        self.data["attempts"].append(
            {
                "number": number,
                "started_at": started_at,
                "finished_at": None,
                "latency_ms": None,
                "outcome": "started",
                "http_status": None,
                "request_id": None,
            }
        )
        self._persist()

    def finish_attempt(
        self,
        number: int,
        *,
        finished_at: str,
        latency_ms: float,
        outcome: str,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        matching = [
            item for item in self.data["attempts"] if item.get("number") == number
        ]
        if len(matching) != 1 or matching[0].get("outcome") != "started":
            raise RerankLogError("Could not match rerank attempt recording.")
        matching[0].update(
            {
                "finished_at": finished_at,
                "latency_ms": latency_ms,
                "outcome": outcome,
                "http_status": status,
                "request_id": request_id,
            }
        )
        self._persist()

    def finish_success(
        self,
        ranked: list[RetrievedChunk],
        selected: tuple[RetrievedChunk, ...],
        drop_reasons: dict[str, str],
        *,
        latency_ms: float,
        response_id: str | None,
        provider: str | None,
        usage: dict | None,
    ) -> None:
        chosen = {item.chunk.id: index for index, item in enumerate(selected, 1)}
        self.data["candidates"] = [
            {
                **_candidate(item, text_hash=_digest(item.chunk.text)),
                "selected": item.chunk.id in chosen,
                "final_position": chosen.get(item.chunk.id),
                "drop_reason": drop_reasons.get(item.chunk.id),
            }
            for item in ranked
        ]
        self.data.update(
            {
                "status": "applied",
                "finished_at": _now(),
                "latency_ms": latency_ms,
                "request_id": response_id,
                "served_by": provider,
                "usage": safe_usage(usage),
            }
        )
        self._persist()
        self._latest()

    def finish_failure(self, *, outcome: str, latency_ms: float) -> None:
        self.data.update(
            {
                "status": "failed",
                "failure_category": outcome,
                "finished_at": _now(),
                "latency_ms": latency_ms,
            }
        )
        self._persist()
        self._latest()

    def _persist(self) -> None:
        try:
            _atomic_write(self.run_path, _json(self.data))
        except Exception:  # noqa: BLE001 - sanitize filesystem details
            raise RerankLogError("Could not persist rerank recording.") from None

    def _latest(self) -> None:
        try:
            _atomic_write(
                self.root / "latest.json",
                _json(
                    {
                        "run_id": self.data["run_id"],
                        "run": str(self.run_path.relative_to(self.root)),
                        "status": self.data["status"],
                    }
                ),
            )
        except Exception:  # noqa: BLE001 - latest is mandatory by contract
            raise RerankLogError("Could not update latest rerank recording.") from None


def _candidate(item: RetrievedChunk, *, text_hash: str) -> dict:
    return {
        "chunk_id": item.chunk.id,
        "text_sha256": text_hash,
        "vector_score": item.vector_score,
        "vector_rank": item.vector_rank,
        "rerank_score": item.rerank_score,
        "rerank_rank": item.rerank_rank,
    }


def safe_usage(value: dict | None) -> dict | None:
    """Keep only documented scalar accounting fields from provider usage."""
    if not isinstance(value, dict):
        return None
    allowed = {
        "prompt_tokens",
        "input_tokens",
        "completion_tokens",
        "total_tokens",
        "cost",
        "is_byok",
    }
    result = {}
    for key, item in value.items():
        if key in allowed and (
            item is None
            or isinstance(item, (int, bool))
            or isinstance(item, float) and math.isfinite(item)
        ):
            result[key] = item
    return result


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
