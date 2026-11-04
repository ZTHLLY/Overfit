"""Explicit, resumable dual-track rerank evaluation.

This module never constructs a provider client.  The CLI live gate creates and
injects the already-audited :class:`RerankService`; tests inject an offline one.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from overfit.errors import RerankError
from overfit.evaluation.rerank import (
    EvalRerankRequest,
    RerankEvalCache,
    RerankEvalTrack,
    build_eval_query,
)
from overfit.models import RetrievedChunk
from overfit.query.rerankers.base import SelectionDecision
from overfit.query.rerankers.logging import safe_endpoint_identity, safe_usage
from overfit.query.rerankers.service import RerankService, rank_response

_CANDIDATE_COUNT = 40
_FINAL_COUNT = 10
_TRACKS = (
    RerankEvalTrack.QUESTION_ONLY,
    RerankEvalTrack.QUESTION_WITH_OPTIONS,
)


class RerankEvalRunError(ValueError):
    """The evaluation input or resume identity is invalid."""


def run_rerank_evaluation(
    *,
    course: str,
    dataset_path: Path,
    store: Any,
    embedder: Any,
    reranker: RerankService,
    output_dir: Path,
    execute: bool,
    resume: Path | None = None,
    no_cache: bool = False,
    index_path: Path | None = None,
) -> dict:
    """Run the frozen 40→10 evaluation sequentially, or resume it explicitly."""
    if not execute:
        raise RerankEvalRunError("Rerank evaluation requires explicit --execute.")

    dataset_path = Path(dataset_path).expanduser().resolve()
    items = _load_dataset(dataset_path)
    output_dir = Path(output_dir).expanduser().resolve()
    adapter = reranker.adapter
    endpoint = safe_endpoint_identity(getattr(adapter, "base_url", None))
    if endpoint is None:
        raise RerankEvalRunError("Rerank evaluation needs a safe HTTPS endpoint.")
    identity = {
        "course": course,
        "dataset_path": str(dataset_path),
        "dataset_sha256": _file_sha256(dataset_path),
        "dataset_count": len(items),
        "index": _index_identity(index_path),
        "embed_model": getattr(embedder, "model", None),
        "embed_dimension": getattr(embedder, "dimension", None),
        "provider": adapter.provider,
        "model": adapter.model,
        "endpoint": endpoint,
        "timeout": reranker.timeout,
        "candidate_count": _CANDIDATE_COUNT,
        "final_count": _FINAL_COUNT,
        "tracks": [track.value for track in _TRACKS],
        "order": "track-then-dataset",
        "concurrency": 1,
    }

    if resume is None:
        run_dir = _new_run_dir(output_dir)
        data = {
            "schema_version": 1,
            "protocol": "rerank-eval-v1",
            "run_id": run_dir.name.removeprefix("rerank-"),
            "run_dir": run_dir,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "resume_count": 0,
            "manifest": identity,
            "records": [],
            "failures": [],
            "summary": {},
            "api": {},
        }
    else:
        run_dir = _resume_dir(resume)
        data = _load_results(run_dir)
        if data.get("manifest") != identity:
            raise RerankEvalRunError(
                "Resume identity does not match dataset, index, model or protocol."
            )
        if data.get("status") == "complete":
            raise RerankEvalRunError("The requested rerank evaluation is already complete.")
        _validate_resume_records(data, items)
        data["resume_count"] = int(data.get("resume_count", 0)) + 1
        data["status"] = "running"
        data["finished_at"] = None

    cache = RerankEvalCache(output_dir)
    completed = {(item["id"], item["track"]) for item in data["records"]}
    _refresh(data, items, run_dir)

    for track in _TRACKS:
        for item in items:
            key = (item["id"], track.value)
            if key in completed:
                continue
            query = build_eval_query(item, track)
            started = time.monotonic()
            try:
                candidates = store.search_candidates(
                    embedder.embed_one(query), limit=_CANDIDATE_COUNT
                )
                if not candidates:
                    raise RerankEvalRunError("Dense retrieval returned no candidates.")
                request = EvalRerankRequest(
                    provider=adapter.provider,
                    model=adapter.model,
                    base_url=adapter.base_url,
                    query=query,
                    candidate_ids=tuple(hit.chunk.id for hit in candidates),
                    documents=tuple(hit.chunk.text for hit in candidates),
                    timeout=reranker.timeout,
                )
                cached = cache.load(request, no_cache=no_cache)
                if cached is not None:
                    ranked = rank_response(candidates, cached.scores)
                    response = cached
                    attempts = 0
                    api_calls = 0
                    latency_ms = 0.0
                    from_cache = True
                else:
                    result = reranker.apply(
                        query,
                        candidates,
                        command="eval-rerank",
                        selector=lambda ranked: _top_selection(ranked),
                        candidate_budget=_CANDIDATE_COUNT,
                        final_budget=_FINAL_COUNT,
                    )
                    if result.response is None:
                        raise RerankEvalRunError("Rerank service omitted its response.")
                    ranked = list(result.ranked)
                    response = result.response
                    attempts = result.attempts
                    api_calls = result.attempts
                    latency_ms = result.latency_ms
                    from_cache = False
                    cache.store(request, response)
                data["records"].append(
                    _record(
                        item,
                        track,
                        query,
                        candidates,
                        ranked,
                        from_cache=from_cache,
                        attempts=attempts,
                        api_calls=api_calls,
                        latency_ms=latency_ms,
                        response=response,
                    )
                )
                completed.add(key)
                _refresh(data, items, run_dir)
            except KeyboardInterrupt:
                data["failures"].append(
                    {
                        "id": item["id"],
                        "track": track.value,
                        "at": _now(),
                        # The interrupt can arrive after dispatch. Without a
                        # provider acknowledgement, zero would be a lie.
                        "attempts": None,
                        "api_calls": None,
                        "latency_ms": round(
                            (time.monotonic() - started) * 1000, 3
                        ),
                        "category": "shutdown",
                        "error_type": "KeyboardInterrupt",
                    }
                )
                data["status"] = "interrupted"
                data["finished_at"] = _now()
                _refresh(data, items, run_dir)
                raise
            except Exception as exc:  # noqa: BLE001 - fail closed at run boundary
                attempts = exc.attempts if isinstance(exc, RerankError) else 0
                data["failures"].append(
                    {
                        "id": item["id"],
                        "track": track.value,
                        "at": _now(),
                        "attempts": attempts,
                        "api_calls": attempts,
                        "latency_ms": round((time.monotonic() - started) * 1000, 3),
                        "category": _failure_category(exc),
                        "error_type": type(exc).__name__,
                    }
                )
                data["status"] = "incomplete"
                data["finished_at"] = _now()
                _refresh(data, items, run_dir)
                return _public(data, run_dir)

    data["status"] = "complete"
    data["finished_at"] = _now()
    _refresh(data, items, run_dir)
    return _public(data, run_dir)


def _load_dataset(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RerankEvalRunError("Could not read rerank evaluation dataset.") from exc
    if not isinstance(value, list) or not value:
        raise RerankEvalRunError("Rerank evaluation dataset must be a non-empty list.")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise RerankEvalRunError("Every evaluation item needs a text id.")
        if item["id"] in seen:
            raise RerankEvalRunError("Evaluation item ids must be unique.")
        seen.add(item["id"])
        _gold_sources(item)  # validate before any provider call
        build_eval_query(item, RerankEvalTrack.QUESTION_ONLY)
        build_eval_query(item, RerankEvalTrack.QUESTION_WITH_OPTIONS)
    return value


def _gold_sources(item: dict) -> list[dict]:
    correct = item.get("correct_option")
    option_sources = item.get("option_sources")
    raw = list(item.get("question_sources") or [])
    if not isinstance(option_sources, dict) or correct not in option_sources:
        raise RerankEvalRunError("Evaluation item lacks correct-option sources.")
    raw.extend(option_sources[correct])
    unique = []
    seen = set()
    for source in raw:
        if not isinstance(source, dict):
            raise RerankEvalRunError("Evaluation gold sources must be objects.")
        pdf, page = source.get("pdf"), source.get("page")
        if not isinstance(pdf, str) or type(page) is not int or page < 1:
            raise RerankEvalRunError("Evaluation gold sources need pdf and page.")
        key = (pdf, page)
        if key not in seen:
            unique.append({"pdf": pdf, "page": page})
            seen.add(key)
    if not unique:
        raise RerankEvalRunError("Evaluation item has no gold sources.")
    return unique


def _matches(hit: RetrievedChunk, gold: list[dict]) -> list[dict]:
    end = hit.chunk.page_end or hit.chunk.page
    return [
        source
        for source in gold
        if hit.chunk.source == source["pdf"]
        and hit.chunk.page <= source["page"] <= end
    ]


def _first_gold_rank(hits: list[RetrievedChunk], gold: list[dict]) -> int | None:
    for rank, hit in enumerate(hits, 1):
        if _matches(hit, gold):
            return rank
    return None


def _record(
    item: dict,
    track: RerankEvalTrack,
    query: str,
    candidates: list[RetrievedChunk],
    ranked: list[RetrievedChunk],
    *,
    from_cache: bool,
    attempts: int,
    api_calls: int,
    latency_ms: float,
    response: Any,
) -> dict:
    gold = _gold_sources(item)
    vector_rank = _first_gold_rank(candidates, gold)
    rerank_rank = _first_gold_rank(ranked, gold)
    usage = safe_usage(response.usage)
    return {
        "id": item["id"],
        "week": item.get("week"),
        "type": item.get("type"),
        "track": track.value,
        "query": query,
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "gold": gold,
        "vector_gold_rank": vector_rank,
        "rerank_gold_rank": rerank_rank,
        "rank_delta": (
            vector_rank - rerank_rank
            if vector_rank is not None and rerank_rank is not None
            else None
        ),
        "api": {
            "success": True,
            "from_cache": from_cache,
            "api_calls": api_calls,
            "attempts": attempts,
            "latency_ms": round(latency_ms, 3),
            "request_id": response.request_id,
            "served_by": response.provider,
            "usage": usage,
        },
        "candidates": [
            {
                "chunk_id": hit.chunk.id,
                "source": hit.chunk.source,
                "page": hit.chunk.page,
                "page_end": hit.chunk.page_end,
                "vector_rank": hit.vector_rank,
                "vector_score": hit.vector_score,
                "rerank_rank": hit.rerank_rank,
                "rerank_score": hit.rerank_score,
                "matched_gold": _matches(hit, gold),
            }
            for hit in ranked
        ],
    }


def _top_selection(ranked: list[RetrievedChunk]) -> SelectionDecision:
    selected = tuple(ranked[:_FINAL_COUNT])
    return SelectionDecision(
        selected=selected,
        drop_reasons={
            item.chunk.id: "outside_final_limit" for item in ranked[len(selected) :]
        },
    )


def _metrics(records: list[dict], rank_key: str) -> dict:
    ranks = [item[rank_key] for item in records]
    count = len(records)
    result = {"n": count}
    for cutoff in (1, 3, 5, 10):
        result[f"hit_at_{cutoff}"] = round(
            sum(rank is not None and rank <= cutoff for rank in ranks) / count, 6
        )
    result["mrr_at_10"] = round(
        sum(1 / rank for rank in ranks if rank is not None and rank <= 10) / count,
        6,
    )
    return result


def _summary(data: dict, items: list[dict]) -> dict:
    expected = len(items)
    result = {}
    for track in _TRACKS:
        records = [item for item in data["records"] if item["track"] == track.value]
        complete = len(records) == expected
        vector = _metrics(records, "vector_gold_rank") if complete else None
        rerank = _metrics(records, "rerank_gold_rank") if complete else None
        result[track.value] = {
            "completed": len(records),
            "expected": expected,
            "metrics_status": "complete" if complete else "incomplete",
            "vector": vector,
            "rerank": rerank,
            "delta": (
                {
                    key: round(rerank[key] - vector[key], 6)
                    for key in ("hit_at_1", "hit_at_3", "hit_at_5", "hit_at_10", "mrr_at_10")
                }
                if complete
                else None
            ),
        }
    return result


def _api_summary(data: dict) -> dict:
    live = [item for item in data["records"] if not item["api"]["from_cache"]]
    cached = [item for item in data["records"] if item["api"]["from_cache"]]
    failures = data["failures"]
    latencies = [item["api"]["latency_ms"] for item in live]
    latencies.extend(
        item["latency_ms"]
        for item in failures
        if _number(item["api_calls"]) > 0
    )
    usages = [item["api"]["usage"] or {} for item in live]
    evaluated_api_requests = len(live) + len(failures)
    return {
        "success_count": len(live),
        "failure_count": len(failures),
        "cache_hit_count": len(cached),
        # Logical provider requests, not retry-level HTTP responses. Cache
        # hits are reported separately and intentionally excluded.
        "success_rate": (
            round(len(live) / evaluated_api_requests, 6)
            if evaluated_api_requests
            else None
        ),
        "api_calls": sum(item["api"]["api_calls"] for item in live)
        + sum(_number(item["api_calls"]) for item in failures),
        "attempts": sum(item["api"]["attempts"] for item in live)
        + sum(_number(item["attempts"]) for item in failures),
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "input_tokens": sum(_number(item.get("input_tokens")) for item in usages),
        "prompt_tokens": sum(_number(item.get("prompt_tokens")) for item in usages),
        "completion_tokens": sum(_number(item.get("completion_tokens")) for item in usages),
        "total_tokens": sum(_number(item.get("total_tokens")) for item in usages),
        "cost": round(sum(float(_number(item.get("cost"))) for item in usages), 12),
    }


def _number(value: Any) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 3)


def _refresh(data: dict, items: list[dict], run_dir: Path) -> None:
    completed = {(item["id"], item["track"]) for item in data["records"]}
    data["next_pending"] = next(
        (
            {"id": item["id"], "track": track.value}
            for track in _TRACKS
            for item in items
            if (item["id"], track.value) not in completed
        ),
        None,
    )
    data["summary"] = _summary(data, items)
    data["api"] = _api_summary(data)
    _atomic_json(run_dir / "results.json", _serializable(data))
    _atomic_text(run_dir / "report.md", _report(data))


def _report(data: dict) -> str:
    lines = [
        "# Rerank evaluation",
        "",
        f"- Status: **{data['status']}**",
        f"- Run ID: `{data['run_id']}`",
        f"- Resume count: {data['resume_count']}",
        f"- API successes / failures / cache hits: {data['api']['success_count']} / {data['api']['failure_count']} / {data['api']['cache_hit_count']}",
        f"- API success rate (cache excluded): {data['api']['success_rate']}",
        f"- API calls / attempts: {data['api']['api_calls']} / {data['api']['attempts']}",
        f"- API latency P50 / P95 (ms): {data['api']['latency_ms_p50']} / {data['api']['latency_ms_p95']}",
        f"- Tokens / cost: {data['api']['total_tokens']} / {data['api']['cost']}",
        "",
        "Formal metrics are emitted only after every item in a track completes; technical failures are never counted as retrieval misses.",
        "",
        "| Track | Completed | Metric status | Vector H@1/3/5/10 | Rerank H@1/3/5/10 | Δ H@1/3/5/10 | Vector/Rerank/Δ MRR@10 |",
        "|---|---:|---|---|---|---|---|",
    ]
    for track in _TRACKS:
        item = data["summary"][track.value]
        if item["vector"] is None:
            vector_hits = rerank_hits = delta_hits = mrr = "—"
        else:
            vector_hits = "/".join(str(item["vector"][f"hit_at_{k}"]) for k in (1, 3, 5, 10))
            rerank_hits = "/".join(str(item["rerank"][f"hit_at_{k}"]) for k in (1, 3, 5, 10))
            delta_hits = "/".join(str(item["delta"][f"hit_at_{k}"]) for k in (1, 3, 5, 10))
            mrr = (
                f"{item['vector']['mrr_at_10']} / "
                f"{item['rerank']['mrr_at_10']} / {item['delta']['mrr_at_10']}"
            )
        lines.append(
            f"| {track.value} | {item['completed']}/{item['expected']} | "
            f"{item['metrics_status']} | {vector_hits} | {rerank_hits} | "
            f"{delta_hits} | {mrr} |"
        )
    return "\n".join(lines) + "\n"


def _failure_category(exc: Exception) -> str:
    if isinstance(exc, RerankError) and exc.failure_category:
        return exc.failure_category
    return "other"


def _validate_resume_records(data: dict, items: list[dict]) -> None:
    """Reject edited/corrupt progress instead of silently skipping work."""
    records = data.get("records")
    failures = data.get("failures")
    if not isinstance(records, list) or not isinstance(failures, list):
        raise RerankEvalRunError("Resume state has invalid records or failures.")
    expected = [
        (item["id"], track.value, build_eval_query(item, track))
        for track in _TRACKS
        for item in items
    ]
    actual = []
    for record in records:
        if not isinstance(record, dict):
            raise RerankEvalRunError("Resume records must be objects.")
        key = (record.get("id"), record.get("track"), record.get("query"))
        api = record.get("api")
        if not isinstance(api, dict) or not isinstance(api.get("from_cache"), bool):
            raise RerankEvalRunError("Resume record API audit is invalid.")
        if api.get("api_calls") not in (0, 1, 2, 3):
            raise RerankEvalRunError("Resume record API call count is invalid.")
        actual.append(key)
    if actual != expected[: len(actual)]:
        raise RerankEvalRunError(
            "Resume records are duplicated, out of order, or outside the dataset."
        )


def _new_run_dir(output_dir: Path) -> Path:
    root = output_dir / "eval"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"rerank-{stamp}-{uuid4()}"
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path.resolve()


def _resume_dir(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    return path.parent if path.name == "results.json" else path


def _load_results(run_dir: Path) -> dict:
    try:
        data = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RerankEvalRunError("Could not read rerank evaluation resume state.") from exc
    data["run_dir"] = run_dir
    return data


def _index_identity(path: Path | None) -> dict | None:
    if path is None:
        return None
    path = Path(path).expanduser().resolve()
    try:
        stat = path.stat()
    except OSError as exc:
        raise RerankEvalRunError("Could not identify the evaluation index.") from exc
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _file_sha256(path),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise RerankEvalRunError("Could not hash evaluation input.") from exc
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _serializable(data: dict) -> dict:
    return {**data, "run_dir": str(data["run_dir"])}


def _public(data: dict, run_dir: Path) -> dict:
    return {**data, "run_dir": run_dir}


def _atomic_json(path: Path, data: dict) -> None:
    _atomic_text(
        path,
        json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
    )


def _atomic_text(path: Path, content: str) -> None:
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
