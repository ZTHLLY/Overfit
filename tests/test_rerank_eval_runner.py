"""Offline contract tests for the explicit P9 rerank evaluation runner."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from overfit import cli
from overfit.errors import RerankExternalError
from overfit.evaluation.rerank_runner import (
    RerankEvalRunError,
    run_rerank_evaluation,
)
from overfit.models import Chunk, RetrievedChunk
from overfit.query.rerankers.base import RerankResponse, RerankScore
from overfit.query.rerankers.service import RerankService


class FakeEmbedder:
    model = "BAAI/bge-m3"
    dimension = 1024

    def embed_one(self, query):
        return query


class FakeStore:
    def __init__(self):
        self.limits = []

    def search_candidates(self, query_vector, *, limit):
        self.limits.append(limit)
        return [
            RetrievedChunk(
                Chunk("wrong", "irrelevant", "other.pdf", 1),
                vector_score=0.9,
                vector_rank=1,
            ),
            RetrievedChunk(
                Chunk("gold", "relevant", "week1/lecture.pdf", 5, page_end=6),
                vector_score=0.8,
                vector_rank=2,
            ),
        ]


@dataclass
class FakeAdapter:
    provider: str = "openrouter"
    model: str = "voyageai/rerank-3"
    base_url: str = "https://openrouter.ai/api/v1/rerank"

    def __post_init__(self):
        self.calls = []

    def rerank(self, query, documents):
        self.calls.append((query, documents))
        return RerankResponse(
            scores=(RerankScore(1, 0.95), RerankScore(0, 0.1)),
            request_id=f"request-{len(self.calls)}",
            provider="voyageai",
            usage={"input_tokens": 10, "total_tokens": 10, "cost": 0.001},
        )


def _dataset(path, *, count=1):
    items = []
    for index in range(count):
        items.append(
            {
                "id": f"q{index + 1}",
                "week": "week01",
                "type": "basic",
                "question": f"Question {index + 1}?",
                "options": {"A": "No", "B": "Yes"},
                "correct_option": "B",
                "question_sources": [
                    {"pdf": "week1/lecture.pdf", "page": 6}
                ],
                "option_sources": {
                    "A": [],
                    "B": [{"pdf": "week1/lecture.pdf", "page": 6}],
                },
            }
        )
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def _service(adapter, output_dir):
    return RerankService(
        adapter,
        output_dir=output_dir,
        sleep=lambda _: None,
    )


def test_dual_track_runner_is_sequential_auditable_and_complete(tmp_path):
    adapter = FakeAdapter()
    store = FakeStore()

    result = run_rerank_evaluation(
        course="IFN580_machine_learning",
        dataset_path=_dataset(tmp_path / "eval.json", count=2),
        store=store,
        embedder=FakeEmbedder(),
        reranker=_service(adapter, tmp_path / "outputs"),
        output_dir=tmp_path / "outputs",
        execute=True,
    )

    assert result["status"] == "complete"
    assert len(adapter.calls) == 4
    assert store.limits == [40, 40, 40, 40]
    assert [record["track"] for record in result["records"]] == [
        "question-only",
        "question-only",
        "question+options",
        "question+options",
    ]
    assert adapter.calls[2][0] == "Question 1?\nA. No\nB. Yes"
    assert result["summary"]["question-only"]["rerank"]["hit_at_1"] == 1.0
    assert result["summary"]["question-only"]["vector"]["hit_at_1"] == 0.0
    assert result["summary"]["question-only"]["rerank"]["mrr_at_10"] == 1.0
    assert result["records"][0]["vector_gold_rank"] == 2
    assert result["records"][0]["rerank_gold_rank"] == 1
    assert result["records"][0]["rank_delta"] == 1
    assert result["records"][0]["api"]["from_cache"] is False
    assert result["records"][0]["api"]["api_calls"] == 1
    assert result["api"]["success_count"] == 4
    assert result["api"]["failure_count"] == 0
    assert result["api"]["success_rate"] == 1.0
    assert result["api"]["total_tokens"] == 40
    assert result["api"]["cost"] == 0.004
    assert result["api"]["latency_ms_p50"] is not None
    assert result["api"]["latency_ms_p95"] is not None
    run_dir = result["run_dir"]
    assert (run_dir / "results.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert "API success rate (cache excluded): 1.0" in (
        run_dir / "report.md"
    ).read_text()
    assert "secret" not in (run_dir / "results.json").read_text().lower()


def test_exact_cache_hit_has_no_provider_call(tmp_path):
    dataset = _dataset(tmp_path / "eval.json")
    first = FakeAdapter()
    common = {
        "course": "IFN580_machine_learning",
        "dataset_path": dataset,
        "store": FakeStore(),
        "embedder": FakeEmbedder(),
        "output_dir": tmp_path / "outputs",
        "execute": True,
    }
    run_rerank_evaluation(
        **common,
        reranker=_service(first, tmp_path / "outputs"),
    )
    second = FakeAdapter()

    result = run_rerank_evaluation(
        **common,
        reranker=_service(second, tmp_path / "outputs"),
    )

    assert second.calls == []
    assert all(record["api"]["from_cache"] for record in result["records"])
    assert all(record["api"]["api_calls"] == 0 for record in result["records"])
    assert result["api"]["cache_hit_count"] == 2
    assert result["api"]["success_count"] == 0
    assert result["api"]["success_rate"] is None


def test_no_cache_forces_new_provider_calls(tmp_path):
    dataset = _dataset(tmp_path / "eval.json")
    common = {
        "course": "IFN580_machine_learning",
        "dataset_path": dataset,
        "store": FakeStore(),
        "embedder": FakeEmbedder(),
        "output_dir": tmp_path / "outputs",
        "execute": True,
    }
    run_rerank_evaluation(
        **common,
        reranker=_service(FakeAdapter(), tmp_path / "outputs"),
    )
    adapter = FakeAdapter()

    result = run_rerank_evaluation(
        **common,
        reranker=_service(adapter, tmp_path / "outputs"),
        no_cache=True,
    )

    assert len(adapter.calls) == 2
    assert all(not record["api"]["from_cache"] for record in result["records"])
    assert result["api"]["api_calls"] == 2


def test_failure_is_incomplete_without_formal_metrics_and_explicit_resume(tmp_path):
    class FailAfterFirst(FakeAdapter):
        def rerank(self, query, documents):
            if len(self.calls) >= 1:
                self.calls.append((query, documents))
                raise RerankExternalError(status=503)
            return super().rerank(query, documents)

    dataset = _dataset(tmp_path / "eval.json", count=2)
    adapter = FailAfterFirst()
    first = run_rerank_evaluation(
        course="IFN580_machine_learning",
        dataset_path=dataset,
        store=FakeStore(),
        embedder=FakeEmbedder(),
        reranker=_service(adapter, tmp_path / "outputs"),
        output_dir=tmp_path / "outputs",
        execute=True,
        no_cache=True,
    )

    assert first["status"] == "incomplete"
    assert len(first["records"]) == 1
    assert first["failures"][0]["attempts"] == 3
    assert first["failures"][0]["category"] == "external_or_timeout"
    assert first["api"]["success_rate"] == 0.5
    assert first["next_pending"] == {"id": "q2", "track": "question-only"}
    assert first["summary"]["question-only"]["metrics_status"] == "incomplete"
    assert first["summary"]["question-only"]["vector"] is None
    assert first["summary"]["question-only"]["rerank"] is None

    healthy = FakeAdapter()
    resumed = run_rerank_evaluation(
        course="IFN580_machine_learning",
        dataset_path=dataset,
        store=FakeStore(),
        embedder=FakeEmbedder(),
        reranker=_service(healthy, tmp_path / "outputs"),
        output_dir=tmp_path / "outputs",
        execute=True,
        resume=first["run_dir"],
        no_cache=True,
    )

    assert resumed["status"] == "complete"
    assert len(resumed["records"]) == 4
    assert len(healthy.calls) == 3  # the already completed request was not replayed
    assert resumed["resume_count"] == 1
    assert resumed["next_pending"] is None


def test_interrupt_records_unknown_call_accounting(tmp_path):
    class Interrupted(FakeAdapter):
        def rerank(self, query, documents):
            raise KeyboardInterrupt

    output_dir = tmp_path / "outputs"
    with pytest.raises(KeyboardInterrupt):
        run_rerank_evaluation(
            course="IFN580_machine_learning",
            dataset_path=_dataset(tmp_path / "eval.json"),
            store=FakeStore(),
            embedder=FakeEmbedder(),
            reranker=_service(Interrupted(), output_dir),
            output_dir=output_dir,
            execute=True,
        )

    run_dir = next((output_dir / "eval").glob("rerank-*"))
    data = json.loads((run_dir / "results.json").read_text())
    assert data["status"] == "interrupted"
    assert data["failures"][0]["category"] == "shutdown"
    assert data["failures"][0]["attempts"] is None
    assert data["failures"][0]["api_calls"] is None


def test_resume_rejects_duplicate_or_out_of_order_records(tmp_path):
    class FailAfterFirst(FakeAdapter):
        def rerank(self, query, documents):
            if self.calls:
                self.calls.append((query, documents))
                raise RerankExternalError(status=503)
            return super().rerank(query, documents)

    output_dir = tmp_path / "outputs"
    dataset = _dataset(tmp_path / "eval.json", count=2)
    partial = run_rerank_evaluation(
        course="IFN580_machine_learning",
        dataset_path=dataset,
        store=FakeStore(),
        embedder=FakeEmbedder(),
        reranker=_service(FailAfterFirst(), output_dir),
        output_dir=output_dir,
        execute=True,
        no_cache=True,
    )
    results_path = partial["run_dir"] / "results.json"
    tampered = json.loads(results_path.read_text())
    tampered["records"].append(tampered["records"][0])
    results_path.write_text(json.dumps(tampered), encoding="utf-8")
    healthy = FakeAdapter()

    with pytest.raises(RerankEvalRunError, match="duplicated, out of order"):
        run_rerank_evaluation(
            course="IFN580_machine_learning",
            dataset_path=dataset,
            store=FakeStore(),
            embedder=FakeEmbedder(),
            reranker=_service(healthy, output_dir),
            output_dir=output_dir,
            execute=True,
            resume=partial["run_dir"],
            no_cache=True,
        )
    assert healthy.calls == []


def test_execute_gate_precedes_any_work(tmp_path):
    adapter = FakeAdapter()
    try:
        run_rerank_evaluation(
            course="C",
            dataset_path=tmp_path / "missing.json",
            store=FakeStore(),
            embedder=FakeEmbedder(),
            reranker=_service(adapter, tmp_path),
            output_dir=tmp_path,
            execute=False,
        )
    except ValueError as exc:
        assert "--execute" in str(exc)
    else:
        raise AssertionError("explicit live gate was not enforced")
    assert adapter.calls == []


def test_cli_requires_execute_before_reading_settings(monkeypatch):
    def forbidden():
        raise AssertionError("settings/.env must not be read without --execute")

    monkeypatch.setattr(cli, "get_settings", forbidden)
    result = CliRunner().invoke(
        cli.app,
        ["eval", "rerank", "--course", "IFN580_machine_learning"],
    )

    assert result.exit_code == 1
    assert "No calls made" in result.output
    assert "--execute" in result.output
