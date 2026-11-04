"""Opt-in generation audit lifecycle; ordinary mock generation is unchanged."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from overfit.errors import RerankError
from overfit.evaluation.citations import check_citation
from overfit.evaluation.report import render_report, summarize
from overfit.evaluation.trace import TraceWriter, collect_code_identity, safe_error
from overfit.query.rerankers.logging import safe_endpoint_identity


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def read_index_identity(path: Path) -> dict:
    """Read metadata and document hashes in one read-only SQLite transaction.

    This is a document-manifest identity, never a claim to have captured a
    complete database snapshot or the historical model/parser binary.
    """
    path = path.resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        meta = dict(connection.execute("SELECT key, value FROM meta ORDER BY key"))
        documents = [
            {"source": source, "hash": digest}
            for source, digest in connection.execute(
                "SELECT source, hash FROM documents ORDER BY source"
            )
        ]
        identity = {"meta": meta, "documents": documents}
        return {
            "path": str(path), **identity,
            "document_manifest_sha256": hashlib.sha256(_canonical(identity).encode()).hexdigest(),
            "snapshot_sha256": None, "parser_digest": None, "model_digest": None,
            "unknown_reason": "Historical parser/model digests and database snapshot unavailable.",
        }
    finally:
        connection.close()


def run_mock_trace(settings, open_store_callback, *, course: str, questions: int,
                   topic: str | None, material: int, reranker=None,
                   on_rerank=None) -> dict:
    """Persist intent first, commit last, and fail closed on any recording error."""
    trace = None
    stage = "initialization"
    try:
        if questions <= 0 or material < 0:
            raise ValueError("Invalid trace request counts")
        # Validate artifact basename before any service may be contacted.
        paper_name = settings.output_path(course, "mock_exam.md").name
        answers_name = settings.output_path(course, "answers.md").name
        budget = material or (10 if topic else questions * 2)
        if topic and reranker is None:
            from overfit.errors import RerankConfigurationError

            raise RerankConfigurationError(
                "A reranker is required for topic trace generation."
            )
        rerank_adapter = getattr(reranker, "adapter", None)
        rerank_provider = getattr(rerank_adapter, "provider", None)
        rerank_model = getattr(rerank_adapter, "model", None)
        rerank_endpoint = safe_endpoint_identity(
            getattr(rerank_adapter, "base_url", None)
        )
        rerank_timeout = getattr(reranker, "timeout", None)
        trace = TraceWriter.create(
            settings.outputs_dir,
            {"course": course, "topic": topic, "questions": questions,
             "material_budget": budget},
            {"generation_model": settings.llm.model,
             "embedding_model": settings.embed.model,
             "rerank_provider": rerank_provider,
             "rerank_model": rerank_model,
             "rerank_endpoint": rerank_endpoint,
             "rerank_timeout": rerank_timeout,
             "generation_endpoint": settings.llm.base_url,
             "embedding_endpoint": settings.embed.base_url,
             "temperature": settings.temperature,
             "request_timeout": settings.request_timeout,
             "max_attempts": settings.max_retries + 1},
            collect_code_identity(Path(__file__).resolve().parents[3]),
        )
        # Imports do not construct clients, and stay out of offline replay.
        from overfit.query import generator, retriever

        stage = "index"
        store, embedder, _ = open_store_callback(course)
        with store:
            stage = "selection"
            index = read_index_identity(settings.db_path(course))
            chunks = retriever.gather_material(
                store,
                embedder,
                count=budget,
                topic=topic,
                reranker=reranker,
                on_rerank=on_rerank,
            )
            serialized = [asdict(chunk) for chunk in chunks]
            check_citation({"page": 1, "source": ""}, serialized)
            trace.emit("materials_selected", {
                "chunks": serialized,
                "selection": {"mode": "topic" if topic else "course", "topic": topic,
                              "requested_count": budget, "actual_count": len(chunks)},
                "index": index,
                "materials_sha256": hashlib.sha256(_canonical(serialized).encode()).hexdigest(),
            })
        stage = "generation"
        result = generator.Generator(settings.llm).mock_exam(
            course, chunks, count=questions, topic=topic,
            max_attempts=settings.max_retries + 1, recorder=trace,
        )
        stage = "artifacts"
        summary = summarize(trace.manifest, trace.events)
        trace.write_artifact("result.json", _canonical(summary) + "\n")
        trace.write_artifact("report.md", render_report(summary))
        if result.exam.items:
            paper, answers = generator.render_exam(course, result.exam)
            trace.write_artifact(paper_name, paper)
            trace.write_artifact(answers_name, answers)
        status = "completed" if result.exam.items else "empty"
        trace.finish(status, "complete", summary["generation_outcome"])
        return {
            "run_dir": str(trace.run_dir),
            "status": status,
            "exit_code": 0,
            "error": None,
            "rerank_failed": False,
            "rerank_attempts": 0,
        }
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - audit lifecycle boundary
        interrupted = isinstance(exc, KeyboardInterrupt)
        status, exit_code = ("interrupted", 130) if interrupted else ("failed", 1)
        error = safe_error(exc)
        if trace is not None and not trace.failed and not trace.finished:
            try:
                outcome = summarize(trace.manifest, trace.events)["generation_outcome"]
                # A finished API response alone cannot prove generation failed:
                # validation or a fallback could still have followed. Only the
                # caught exhaustion exception supplies that terminal fact.
                if (stage == "generation" and isinstance(exc, generator.GenerationError)
                        and not any(event["event_type"] == "validation_finished"
                                    and event["payload"]["status"] == "valid"
                                    for event in trace.events)):
                    outcome = "failed"
                trace.finish(status, stage, outcome, error=error)
            except (Exception, KeyboardInterrupt) as recording_error:  # noqa: BLE001 - best effort only
                error = safe_error(recording_error)
                # An interrupted process still exits 130, even when best-effort
                # recording fails; neither path announces a successful trace.
        rerank_failed = isinstance(exc, RerankError)
        return {
            "run_dir": str(trace.run_dir) if trace is not None else None,
            "status": status,
            "exit_code": exit_code,
            "error": error,
            "rerank_failed": rerank_failed,
            "rerank_attempts": exc.attempts if rerank_failed else 0,
        }


def run_fixture_trace(
    settings,
    *,
    course: str,
    topic: str | None,
    questions: int,
    chunks: list,
    fixture_identity: dict,
    max_attempts: int,
    max_calls: int,
    generator_factory=None,
) -> dict:
    """Generate one standard trace from an isolated, ordered material fixture."""
    trace = None
    stage = "initialization"
    try:
        if questions <= 0 or max_attempts <= 0 or max_calls <= 0 or not chunks:
            raise ValueError("Invalid fixture generation limits")
        paper_name = settings.output_path(course, "mock_exam.md").name
        answers_name = settings.output_path(course, "answers.md").name
        trace = TraceWriter.create(
            settings.outputs_dir,
            {
                "course": course,
                "topic": topic,
                "questions": questions,
                "material_budget": len(chunks),
                "task_id": fixture_identity["task_id"],
            },
            {
                "generation_model": settings.llm.model,
                "generation_endpoint": settings.llm.base_url,
                "temperature": settings.temperature,
                "request_timeout": settings.request_timeout,
                "max_attempts": max_attempts,
            },
            collect_code_identity(Path(__file__).resolve().parents[3]),
        )
        serialized = [asdict(chunk) for chunk in chunks]
        check_citation({"page": 1, "source": ""}, serialized)
        trace.emit(
            "materials_selected",
            {
                "chunks": serialized,
                "selection": {
                    "mode": "generator_challenge_fixture",
                    "topic": topic,
                    "requested_count": len(chunks),
                    "actual_count": len(chunks),
                },
                "index": {
                    "kind": "generator_challenge_fixture",
                    "suite_id": fixture_identity["suite_id"],
                    "case_id": fixture_identity["case_id"],
                    "inputs_sha256": fixture_identity["inputs_sha256"],
                    "case_sha256": fixture_identity["case_sha256"],
                },
                "materials_sha256": hashlib.sha256(
                    _canonical(serialized).encode()
                ).hexdigest(),
            },
        )
        from overfit.query import generator

        stage = "generation"
        factory = generator_factory or (lambda llm: generator.Generator(llm))
        result = factory(settings.llm).mock_exam(
            course,
            chunks,
            count=questions,
            topic=topic,
            max_attempts=max_attempts,
            max_calls=max_calls,
            recorder=trace,
        )
        stage = "artifacts"
        summary = summarize(trace.manifest, trace.events)
        trace.write_artifact("result.json", _canonical(summary) + "\n")
        trace.write_artifact("report.md", render_report(summary))
        if result.exam.items:
            paper, answers = generator.render_exam(course, result.exam)
            trace.write_artifact(paper_name, paper)
            trace.write_artifact(answers_name, answers)
        status = "completed" if result.exam.items else "empty"
        trace.finish(status, "complete", summary["generation_outcome"])
        call_count = sum(
            event["event_type"] == "call_started" for event in trace.events
        )
        return {
            "run_dir": str(trace.run_dir),
            "run_id": trace.run_id,
            "status": status,
            "exit_code": 0,
            "error": None,
            "call_count": call_count,
            "summary": summary,
        }
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
        interrupted = isinstance(exc, KeyboardInterrupt)
        status, exit_code = ("interrupted", 130) if interrupted else ("failed", 1)
        error = safe_error(exc)
        if trace is not None and not trace.failed and not trace.finished:
            try:
                outcome = summarize(trace.manifest, trace.events)["generation_outcome"]
                if stage == "generation":
                    outcome = "failed"
                trace.finish(status, stage, outcome, error=error)
            except (Exception, KeyboardInterrupt) as recording_error:  # noqa: BLE001
                error = safe_error(recording_error)
        return {
            "run_dir": str(trace.run_dir) if trace is not None else None,
            "run_id": trace.run_id if trace is not None else None,
            "status": status,
            "exit_code": exit_code,
            "error": error,
            "call_count": (
                sum(event["event_type"] == "call_started" for event in trace.events)
                if trace is not None
                else 0
            ),
            "summary": None,
        }
